"""Append-only JSONL record of everything the bridge saw and produced.

Why this is worth more than it looks. Every question settled on real hardware
today was settled by replaying a recording offline, and every question answered
by a live A/B was answered wrongly: the same candidate rotation scored 57 % and
4.7 % on consecutive runs because the operator moved differently in between.
A recording removes the operator from the comparison, and then keeps paying --
today's two sessions became the fixtures that verified the wrist mapping, found
the Euler glitch, and killed three of my own wrong explanations.

What it stores, per frame: the raw input (hands, head) AND the targets the
retargeter produced from it. Both halves matter. Input alone lets you re-run
the pipeline; input plus output lets you diff a code change against what the
robot was actually told, which is the question during a regression.

Two design points worth stating, because the obvious implementations are
wrong:

Writing happens on a background thread behind a bounded queue. The frame path
is an asyncio handler at 90 fps, and a disk write inside it stalls every hand
frame -- the recorder would degrade the thing it exists to measure.

When the queue backs up, frames are DROPPED and counted, never blocked. A
recorder that can stall teleop is a hazard; one that admits to a gap is just a
recorder with a gap. The count goes in the session footer so the gap is
visible at analysis time rather than silently changing a frame rate.

stop() drains before it closes. The footer's counts are a promise the seal
checks (footer.frames == rows in the file), so stop() waits -- bounded by
STOP_DRAIN_S -- for the writer to land what the session already queued, then
drops and counts whatever is left, then writes the footer. Every queued item
carries its session id; an item whose session has since closed is a counted
no-op (`late`), never a write against a closed handle. Before this, a camera
frame popped just as stop() cleared the frames directory raised in the writer
thread and killed it, silently, for the rest of the process.
"""
import copy
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque

# The task lifecycle, from the g1-coke-demo README's operator sequence:
#   side rest -> approach -> pregrasp -> grasp -> lift -> hold/present
#             -> clear table -> lower/place -> side rest -> release
# One word per step, and "rest" once: an outcome's phase names the FURTHEST
# point the attempt reached, so the return to rest is "place" then "release".
# Kept here, not in the server, because the recorder is what refuses a phase
# nobody downstream would recognise -- a label that is free text is a label
# nobody can count.
PHASES = ("rest", "approach", "pregrasp", "grasp", "lift", "present",
          "clear", "place", "release")
# success: the can went where the task says. fail: it did not. abort: the
# operator stopped the attempt before it could be judged (a dropout, a spotter's
# call); an abort is not a failure of the policy the demonstration is for.
RESULTS = ("success", "fail", "abort")
NOTE_MAX = 200


class SessionRecorder:
    ARTIFACT_BYTES_MAX = 32 * 1024 * 1024
    # Bounded so a stalled disk costs memory in a straight line rather than
    # exponentially. At 90 fps this is about 22 s of slack.
    QUEUE_MAX = 2000
    CAM_TAIL_S = 3.0
    """Seconds after the last ARMED frame that camera frames are still kept.

    The release is part of the demonstration: the hand opens and the can is
    put down after the arm has already been told to hold. Zero would cut the
    end off every attempt, which is the half a trainer needs to see whether
    the grasp actually worked."""

    # How long stop() waits for the writer to finish what THIS session already
    # queued before it writes the footer. Long enough for a few hundred rows
    # and a burst of camera frames on a healthy disk; short enough that a
    # stuck disk cannot hold the operator's stop hostage. What is still
    # queued when it expires is dropped and COUNTED in the footer -- the seal
    # relies on footer.frames == rows in the file, and a footer written while
    # rows were still landing would break that on every stop.
    STOP_DRAIN_S = 2.0

    @staticmethod
    def pick_dir(explicit=""):
        """First writable candidate wins, and says so.

        A `persist` entitlement mounts somewhere the app survives a redeploy,
        but the mount point is the device's business, not ours. Probing beats
        hardcoding a guess that fails silently and puts a session's worth of
        data somewhere it is about to be deleted.
        """
        cands = [explicit] if explicit else []
        # A named persist volume mounts at /<name>; ours is "sessions"
        # (wendy.json). The rest are fallbacks for other hosts.
        cands += ["/sessions", "/data/sessions", "/persist/sessions",
                  "/var/lib/app/sessions", "/workspace/sessions",
                  os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "sessions")]
        for c in cands:
            if not c:
                continue
            try:
                os.makedirs(c, exist_ok=True)
                probe = os.path.join(c, ".writable")
                with open(probe, "w") as fh:
                    fh.write("x")
                os.unlink(probe)
            except OSError:
                continue
            persistent = not os.path.abspath(c).startswith(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            print(f"[recorder] sessions -> {c}"
                  + ("" if persistent else "  (inside the image: lost on redeploy)"))
            return c
        print("[recorder] no writable session directory; recording disabled")
        return None

    def __init__(self, directory):
        self.dir = directory
        self._q = deque()
        self._lock = threading.Lock()
        # File/directory lifetime is separate from the queue/hot-path lock.
        # Camera writes may hold this for as long as disk takes; note() never
        # touches it, so a slow persistent volume cannot stale teleop input.
        self._io_lock = threading.Lock()
        self._wake = threading.Event()
        self._fh = None
        self._sid = None
        self._t0 = None
        self.n_written = 0
        self.n_dropped = 0
        self.n_marks = 0
        self.n_outcomes = {r: 0 for r in RESULTS}
        self.n_cam_written = 0
        self.n_cam_dropped = 0
        self.n_depth_written = 0
        self.n_depth_missing = 0     # camera frames that came without depth
        # Camera frames are kept only while the robot is ARMED, plus a short
        # tail. A session left open records the camera for as long as it is
        # open, and one on 2026-09-16 sat open 18.5 minutes for 51 seconds of
        # teleop: 9360 frames, 278 MB, of which 418 were referenced by any
        # pose row and 216 by an ARMED one. The rest is a camera filming an
        # empty table, sealed and pulled and committed along with the rest.
        #
        # ARMED is the right gate because ARMED is what "demonstration" means
        # everywhere else here -- the validator's share, the exporter's rows,
        # the attempt boundaries. A frame from outside it is not training data
        # under any of those definitions.
        self.n_cam_skipped = 0
        self._armed_t = None          # monotonic, when ARMED was last seen
        # What the last session was and when it stopped, so a verdict pressed
        # a moment too late can say which session it missed instead of
        # disappearing. See OUTCOME_GRACE_S.
        self._last_sid = None
        self._last_stop_t = None
        self._frames_dir = None  # <sid>.frames/, created on the first camera frame
        self._index_fh = None
        self._streams_fh = None
        self.n_stream_seen = self.n_stream_written = self.n_stream_dropped = 0
        self.n_artifact_written = self.n_artifact_dropped = self._artifact_bytes = 0
        self._artifact_pending = None
        self._calibrations = []  # [(dict, filename)] written this session, in order
        self._ref_v = None      # version of the last wrist reference written
        self._offset_v = None   # version of the last arming offset written
        # Items carry the session id they belong to, and the writer keeps a
        # count of items it has taken but not finished. stop() waits on this
        # condition until both are clear for its session; a late item whose
        # session is gone is a counted no-op (n_late), never a write against
        # a closed handle and never a reason to touch the session that
        # replaced it. Before this, a camera frame popped just as stop()
        # cleared _frames_dir raised TypeError in _run, which nothing caught
        # -- and killed the writer thread for the rest of the process.
        self._cv = threading.Condition(self._lock)
        self._inflight = 0
        # Network/source reads begin before their response can be queued. Keep
        # those reservations separate from writer items so stop() can either
        # drain them into this session or count them in this session's footer.
        self._capture_seq = 0
        self._capture_inflight = {}  # token -> session id
        self.n_capture_dropped = 0
        self._closing = None     # (sid, deadline) while a stop() is draining
        self.n_late = 0
        self.writer_errors = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="session-recorder")
        self._thread.start()

    # -- control ----------------------------------------------------------- #
    def start(self, name="", meta=None):
        if not self.dir:
            return {"ok": False, "error": "no writable session directory"}
        safe = "".join(c for c in str(name) if c.isalnum() or c in "-_")[:40]
        sid = time.strftime("%Y%m%d-%H%M%S") + (f"-{safe}" if safe else "")
        try:
            os.makedirs(self.dir, exist_ok=True)
            # Never overwrite a take created in the same wall-clock second.
            while True:
                try:
                    fh = open(os.path.join(self.dir, sid + ".jsonl"), "x")
                    break
                except FileExistsError:
                    sid += "-" + uuid.uuid4().hex[:8]
        except OSError as e:
            return {"ok": False, "error": f"cannot open session file: {e}"}
        with self._lock:
            if self._fh is not None:
                self._expire_captures_locked(self._sid)
                self._close_locked()
            self._fh = fh
            self._sid = sid
            self._t0 = time.time()
            self.n_cam_written = 0
            self.n_cam_dropped = 0
            self.n_depth_written = 0
            self.n_depth_missing = 0
            self.n_cam_skipped = 0
            self._armed_t = None
            self._frames_dir = None
            self._index_fh = None
            self._streams_fh = None
            self.n_stream_seen = self.n_stream_written = self.n_stream_dropped = 0
            self.n_artifact_written = self.n_artifact_dropped = self._artifact_bytes = 0
            self.n_capture_dropped = 0
            self.n_late = 0
            self.writer_errors = 0
            self._calibrations = []
            self.n_written = 0
            self.n_dropped = 0
            self.n_marks = 0
            self.n_outcomes = {r: 0 for r in RESULTS}
            self._ref_v = None  # the first frame of a session carries the reference
            self._offset_v = None
            self._q.clear()
            head = {"type": "header", "session": sid, "t_start": self._t0, "v": 1}
            # Facts about the run that every frame shares -- which arm math
            # produced the targets, for one. In the header once, not per frame.
            for k, v in (meta or {}).items():
                head.setdefault(k, v)
            fh.write(json.dumps(head) + "\n")
        return {"ok": True, "session": sid}

    def stop(self, expected_sid=None):
        """Close the session: drain what it queued (bounded), count the rest
        as dropped, write the footer. Blocks up to STOP_DRAIN_S -- callers on
        the event loop run it in a thread (server.stop_session does)."""
        with self._cv:
            if self._fh is None:
                return {"ok": False, "error": "not recording"}
            if expected_sid is not None and expected_sid != self._sid:
                return {"ok": False, "error": "session ownership changed",
                        "expected_session": expected_sid,
                        "current_session": self._sid}
            sid = self._sid
            deadline = time.monotonic() + self.STOP_DRAIN_S
            # Tell the writer when to stand aside. Locks are not fair: after
            # the deadline a writer that keeps popping and holding the lock for
            # camera writes can beat a woken stop() to it several times over,
            # and the "bounded" wait overran by half a second per race. Past
            # the deadline the writer takes nothing more until stop() is done.
            self._closing = (sid, deadline)
            self._wake.set()
            try:
                while self._pending_for(sid) and time.monotonic() < deadline:
                    self._cv.wait(timeout=max(0.0, deadline - time.monotonic()))
            finally:
                self._closing = None
            self._expire_captures_locked(sid)
            # Whatever is still queued for this session did not make it: drop
            # it, and make the counts say so, so footer.frames is exactly the
            # rows in the file and footer.marks exactly the mark rows.
            pending = self._artifact_pending
            if pending is not None and pending["sid"] == sid and not pending["accounted"]:
                self.n_stream_dropped += 1
                self.n_artifact_dropped += 1
                self._artifact_bytes -= pending["bytes"]
                pending["accounted"] = True
            keep = deque()
            for item in self._q:
                if item[1] != sid:
                    keep.append(item)
                    continue
                kind = item[0]
                if kind == "row":
                    self.n_dropped += 1
                elif kind == "cam":
                    self.n_cam_dropped += 1
                elif kind == "stream":
                    self.n_stream_dropped += 1
                elif kind == "artifact":
                    self.n_stream_dropped += 1
                    self.n_artifact_dropped += 1
                    self._artifact_bytes -= len(item[3])
                elif kind == "mark":
                    self.n_marks -= 1
                elif kind == "outcome":
                    self.n_outcomes[item[3]] -= 1
            self._q = keep
            n, dropped = self.n_written, self.n_dropped
            marks = self.n_marks
            outcomes = dict(self.n_outcomes)
            self._last_sid, self._last_stop_t = sid, time.monotonic()
            cam_n, cam_dropped = self.n_cam_written, self.n_cam_dropped
            depth_n, depth_missing = self.n_depth_written, self.n_depth_missing
            cam_skipped = self.n_cam_skipped
            stream_counts = self._stream_counts()
            writer_errors, late = self.writer_errors, self.n_late
            dur = time.time() - (self._t0 or time.time())
            self._close_locked()
        return {"ok": True, "session": sid, "frames": n, "dropped": dropped,
                "marks": marks, "outcomes": outcomes,
                "cam_frames": cam_n, "cam_dropped": cam_dropped,
                "depth_frames": depth_n, "depth_missing": depth_missing,
                # Kept apart from dropped on purpose: a big number here is the
                # camera declining to film an empty table, not a fault.
                "cam_skipped_not_armed": cam_skipped,
                "writer_errors": writer_errors, "late": late,
                **stream_counts,
                "seconds": round(dur, 1)}

    def _pending_for(self, sid):
        """Caller holds the lock: is anything of this session queued or in flight?"""
        return (self._inflight > 0
                or any(owner == sid for owner in self._capture_inflight.values())
                or any(it[1] == sid for it in self._q))

    def begin_capture(self, expected_sid):
        """Reserve a source request for one session before network I/O starts.

        A valid token lets the response finish queuing while stop() is draining.
        If the drain deadline expires, stop() invalidates the token and records
        the lost request in the old session's footer.
        """
        with self._cv:
            if (expected_sid is None or self._sid != expected_sid
                    or self._closing is not None):
                return None
            self._capture_seq += 1
            token = self._capture_seq
            self._capture_inflight[token] = expected_sid
            return token

    def reserve_camera(self):
        """Pin an admitted camera frame before deferred depth work begins."""
        with self._cv:
            if self._sid is None or self._closing is not None:
                return None
            if self._armed_t is None or time.monotonic() - self._armed_t > self.CAM_TAIL_S:
                self.n_cam_skipped += 1
                return None
            self._capture_seq += 1
            token = self._capture_seq
            self._capture_inflight[token] = self._sid
            return self._sid, token

    def finish_capture(self, token):
        """Release a source request reservation and wake a draining stop()."""
        if token is None:
            return False
        with self._cv:
            owner = self._capture_inflight.pop(token, None)
            if owner is None:
                # The owning session already sealed and counted this request.
                self.n_late += 1
                return False
            self._cv.notify_all()
            return True

    def _expire_captures_locked(self, sid):
        expired = [token for token, owner in self._capture_inflight.items()
                   if owner == sid]
        for token in expired:
            del self._capture_inflight[token]
        self.n_capture_dropped += len(expired)

    def _source_admissible_locked(self, expected_sid, capture_token):
        if self._sid is None or (expected_sid is not None and expected_sid != self._sid):
            self.n_late += 1
            return False
        owner = (self._capture_inflight.get(capture_token)
                 if capture_token is not None else None)
        if capture_token is not None and owner != self._sid:
            self.n_late += 1
            return False
        if self._closing is not None and owner != self._sid:
            return False
        return True

    @property
    def recording(self):
        with self._lock:
            return self._sid is not None

    def status(self):
        with self._lock:
            return {"recording": self._sid, "frames": self.n_written,
                    **self._stream_counts(),
                    "dropped": self.n_dropped,
                    "cam_frames": self.n_cam_written, "cam_dropped": self.n_cam_dropped,
                    "cam_skipped_not_armed": self.n_cam_skipped,
                    "seconds": (round(time.time() - self._t0, 1)
                                if self._t0 and self._sid else None),
                    "queued": len(self._q), "late": self.n_late,
                    "writer_errors": self.writer_errors, "dir": self.dir}

    def _stream_counts(self):
        return {"stream_seen": self.n_stream_seen,
                "stream_records": self.n_stream_written,
                "stream_dropped": self.n_stream_dropped,
                "artifact_records": self.n_artifact_written,
                "artifact_dropped": self.n_artifact_dropped,
                "artifact_queued_bytes": self._artifact_bytes,
                "capture_inflight": sum(owner == self._sid
                                        for owner in self._capture_inflight.values()),
                "capture_dropped": self.n_capture_dropped}

    def note_stream(self, stream, payload, *, unix_ns=None, monotonic_ns=None,
                    expected_sid=None, capture_token=None):
        """Archive an independent input/output without resampling it to pose rows.

        JSON wire text is retained verbatim. Other values are snapshotted so
        a publisher reusing a mutable buffer cannot change a queued reading.
        The existing bounded writer owns the file; loss is explicit.
        """
        event = {"stream": stream, "received_at_unix_ns": time.time_ns() if unix_ns is None else unix_ns,
                 "received_at_monotonic_ns": time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
                 "payload": copy.deepcopy(payload)}
        with self._lock:
            if not self._source_admissible_locked(expected_sid, capture_token):
                return
            event["session"] = self._sid
            if capture_token is not None:
                event["capture_id"] = capture_token
            self.n_stream_seen += 1
            event["sequence"] = self.n_stream_seen
            if len(self._q) >= self.QUEUE_MAX:
                self.n_stream_dropped += 1
                return
            self._q.append(("stream", self._sid, event))
        self._wake.set()

    def note_artifact(self, stream, data, format, metadata, *, expected_sid=None,
                      capture_token=None, unix_ns=None, monotonic_ns=None):
        """Queue original response bytes, bounded by bytes as well as item count.

        No caller-supplied filename is used. A response belongs to the session
        that requested it, even if a network request completes after a stop.
        """
        if format not in ("zip", "npz", "jsonl", "bin"):
            raise ValueError("unsupported artifact format")
        data = bytes(data)
        event = {"stream": stream, "payload": copy.deepcopy(metadata),
                 "received_at_unix_ns": time.time_ns() if unix_ns is None else unix_ns,
                 "received_at_monotonic_ns": time.monotonic_ns() if monotonic_ns is None else monotonic_ns}
        with self._lock:
            if not self._source_admissible_locked(expected_sid, capture_token):
                return False
            event["session"] = self._sid
            if capture_token is not None:
                event["capture_id"] = capture_token
            self.n_stream_seen += 1
            event["sequence"] = self.n_stream_seen
            if len(self._q) >= self.QUEUE_MAX or self._artifact_bytes + len(data) > self.ARTIFACT_BYTES_MAX:
                self.n_stream_dropped += 1
                self.n_artifact_dropped += 1
                return False
            event["artifact"] = {"file": f"artifact-{self.n_stream_seen:09d}.{format}",
                                 "bytes": len(data)}
            self._artifact_bytes += len(data)
            self._q.append(("artifact", self._sid, event, data))
        self._wake.set()
        return True
    def sessions(self):
        # os.listdir(None) lists the CURRENT directory rather than failing, so
        # a recorder with nowhere to write would happily report whatever files
        # happen to sit beside the process. Guard explicitly.
        if not self.dir:
            return []
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".jsonl"))
        except OSError:
            return []
        out = []
        for n in names:
            p = os.path.join(self.dir, n)
            try:
                # Sealed or not, per session: the training pipeline consumes
                # only sealed manifests, so the listing says which these are.
                out.append({"session": n[:-6], "bytes": os.path.getsize(p),
                            "manifest": os.path.exists(os.path.join(self.dir, n[:-6] + ".manifest.json"))})
            except OSError:
                pass
        return out

    def manifest_path(self, sid):
        """The sealed manifest for a session id, or None. Same path rules as path()."""
        if not self.dir or not sid or "/" in sid or "\\" in sid or sid.startswith("."):
            return None
        p = os.path.join(self.dir, sid + ".manifest.json")
        return p if os.path.exists(p) else None

    def path(self, sid):
        """Resolve a session id to a file, refusing anything path-like."""
        if not self.dir or not sid or "/" in sid or "\\" in sid or sid.startswith("."):
            return None
        p = os.path.join(self.dir, sid + ".jsonl")
        return p if os.path.exists(p) else None

    # -- the hot path ------------------------------------------------------ #
    def note(self, frame, targets=None, robot=None, wrist_ref=None, cam=None,
             sent=None, arm_offset=None, wrist_gain=None):
        """Queue one frame. Never blocks, never raises, never writes here.

        `robot` is what the onboard last reported about itself -- at minimum
        its state; since body-state recording also `body`: waist + arm q/dq/tau
        in Unitree motor order, the half of a physics replay the robot supplies.
        Written through verbatim. It is at minimum
        its state. Recording it per frame is what makes an episode usable: a
        stretch where the robot was HOLDING has the operator's hands moving and
        the arm standing still, which as training data teaches exactly the
        wrong lesson. Marked, it can be cut. Unmarked, it is indistinguishable
        from a demonstration of not moving.

        `wrist_ref` is the retargeter's wrist zero (G1Retargeter.wrist_reference()).
        The wrist targets are relative to it, so without it a recording's wrist
        joints cannot be reproduced -- replaying 20260915-001254 matched every
        shoulder and elbow exactly and every wrist joint by up to 170 deg, and
        the question that recording raised (a wrist bias, or a bad zero pose?)
        was unanswerable from the file. It is written on the first frame of a
        session and again on the first frame after each change, keyed by its
        version, so it costs a dict compare per frame and nothing more.

        `sent` is the arm command the sink actually queued for this frame
        (RealG1Sink.commanded()), and `arm_offset` the constant it subtracts
        (RealG1Sink.arm_offset()). `targets` is the operator's INTENT; `sent`
        is what the robot was TOLD; the difference is the arming offset,
        captured target-minus-measured on the first ARMED frame (53.8 deg on
        an elbow when measured). A physics replay fed `targets` reproduces a
        lurch the real robot never made, and a commanded-versus-measured
        comparison sees the offset as a permanent error on a joint nothing
        is touching. The offset is written like `wrist_ref`: once per
        version, full precision. None for either omits the key.

        `wrist_gain` is the versioned derived controller-wrist observation
        used before IK. It is written beside the untouched raw wrist in
        `hands`, never instead of it, so the transform can be retuned offline.
        """
        with self._lock:
            if self._sid is None:
                self.n_late += 1        # a frame with no session to belong to
                return
            if len(self._q) >= self.QUEUE_MAX:
                self.n_dropped += 1
                return
            ref = None
            if wrist_ref is not None and wrist_ref.get("v") != self._ref_v:
                ref = wrist_ref
                self._ref_v = wrist_ref.get("v")
            off = None
            if arm_offset is not None and arm_offset.get("v") != self._offset_v:
                off = arm_offset
                self._offset_v = arm_offset.get("v")
            # The camera's gate. The pose rows are the only thing here that
            # sees the robot's state, and they arrive at 90 Hz against the
            # camera's ~8, so this is never stale by more than a frame.
            if str(((robot or {}).get("state")) or "").startswith("ARMED"):
                self._armed_t = time.monotonic()
            self._q.append(("row", self._sid, time.time(), frame, targets, robot, ref, cam,
                            sent, off, wrist_gain))
        self._wake.set()

    def note_camera(self, header, jpeg, depth=None, *, expected_sid=None,
                    capture_token=None):
        """Queue one camera frame. Same rules as note(): never blocks, never
        raises, drops and counts when the writer is behind.

        Frames go BESIDE the session, as <sid>.frames/<seq>.jpg plus an
        index line, never inline: fifteen JPEGs a second in a JSONL row would
        make the file unreadable for everything else. The pose rows carry the
        seq of the frame that was current when they were written (`cam`), so
        joining is a lookup, and its age, so a stale frame is visible.

        `depth` is the same frame's aligned metric depth, if a camera source
        supplies one ({"depth_png", "calibration", "captured_at_unix_ns"}),
        or None when none came. It rides the same queue
        item as the JPEG so the two can never be written for different
        sessions or in different order, and it lands as <seq>.depth.png with
        the index row saying so per frame -- a session with depth on some
        frames and not others is honest frame by frame, not on average.
        """
        with self._lock:
            if not self._source_admissible_locked(expected_sid, capture_token):
                return
            # Only while the robot is ARMED, plus CAM_TAIL_S. SKIPPED, not
            # dropped: dropped means the writer fell behind and the episode
            # has a hole in it, which is a fault. This is the recorder
            # declining to store a picture of an empty table, which is not.
            t = self._armed_t
            # Expanded capture reserves a session token before fetching exact
            # source bytes. Those frames belong to that session even before a
            # pose row marks ARMED; legacy camera callbacks retain the ARMED
            # plus tail storage gate.
            if (capture_token is None
                    and (t is None or time.monotonic() - t > self.CAM_TAIL_S)):
                self.n_cam_skipped += 1
                return
            if len(self._q) >= self.QUEUE_MAX:
                self.n_cam_dropped += 1
                return
            received_ns = header.get("received_at_unix_ns")
            received = received_ns / 1e9 if isinstance(received_ns, int) else time.time()
            self._q.append(("cam", self._sid, received, header, jpeg, depth))
        self._wake.set()

    def mark(self, label=""):
        """Drop a marker row in place: {"type": "mark", "t", "label"}.

        One recording usually holds several attempts, and the real can
        sessions had no way to tell where one ended -- an 85 s pause was the
        only hint. A mark goes through the same queue as the frames, so it
        lands in order between them; replay and the validator split a session
        into attempts at marks and at pauses.
        """
        label = str(label or "")[:80]
        with self._lock:
            if self._sid is None:
                return {"ok": False, "error": "not recording"}
            self._q.append(("mark", self._sid, time.time(), label))
            self.n_marks += 1
            n = self.n_marks
        self._wake.set()
        return {"ok": True, "label": label, "marks": n}

    # How long after a stop a verdict still attaches to the session that just
    # ended. The operator presses A *after* letting go of the can, which is
    # often after the squeeze that stopped the recording -- and until
    # 2026-09-16 that press returned "not recording" and vanished with no sign
    # of it in the headset. A whole session was recorded, the operator pressed
    # A, and the file has no label. Five seconds covers the reach; longer
    # would start attaching verdicts to the wrong attempt.
    OUTCOME_GRACE_S = 5.0

    def outcome(self, result, phase="", note=""):
        """Drop a verdict row in place: {"type": "outcome", "t", "result", "phase", "note"}.

        A mark says where an attempt starts; nothing said how it ended. The
        2026-09-15 can sessions hold seventeen sustained grips and the only
        record of which ones lifted the can was the operator's memory. For
        training the outcome IS the label -- a miss and a lift look alike in
        the joints for their first two seconds -- so the operator presses
        success or fail right after letting go and it lands in order between
        the frames. `phase` is the furthest lifecycle step reached; empty is
        allowed, a word outside PHASES is refused rather than stored, because
        a label that cannot be counted is not a label.
        """
        result = str(result or "")
        phase = str(phase or "")
        if result not in RESULTS:
            return {"ok": False, "error": f"result must be one of {', '.join(RESULTS)}"}
        if phase and phase not in PHASES:
            return {"ok": False, "error": f"phase must be empty or one of {', '.join(PHASES)}"}
        note = str(note or "")[:NOTE_MAX]
        with self._lock:
            if self._sid is None:
                # Just stopped? The verdict belongs to the session that ended.
                # It cannot be written -- that file is closed and sealed -- so
                # say so plainly and name it, rather than returning a bare
                # "not recording" that reads as "you pressed nothing".
                late = self._last_sid if (self._last_stop_t is not None
                                          and time.monotonic() - self._last_stop_t
                                          <= self.OUTCOME_GRACE_S) else None
                if late:
                    return {"ok": False, "late": True, "session": late, "result": result,
                            "error": f"{result} pressed {time.monotonic() - self._last_stop_t:.1f} s "
                                     f"after {late} was stopped and sealed: press it BEFORE "
                                     "stopping, or add it from the control page"}
                return {"ok": False, "error": "not recording"}
            self._q.append(("outcome", self._sid, time.time(), result, phase, note))
            self.n_outcomes[result] += 1
            counts = dict(self.n_outcomes)
        self._wake.set()
        return {"ok": True, "result": result, "phase": phase, "note": note,
                "outcomes": counts}

    # -- writer ------------------------------------------------------------ #
    def _close_locked(self):
        # Caller owns `_lock`; `_io_lock` waits only for the one camera write
        # already in flight.  It is never used by note()/note_camera().
        with self._io_lock:
            try:
                self._fh.write(json.dumps({"type": "footer", "frames": self.n_written,
                                           "dropped": self.n_dropped,
                                           "marks": self.n_marks,
                                           "outcomes": dict(self.n_outcomes),
                                           "cam_frames": self.n_cam_written,
                                           "cam_dropped": self.n_cam_dropped,
                                           "depth_frames": self.n_depth_written,
                                           "depth_missing": self.n_depth_missing,
                                           "cam_skipped_not_armed": self.n_cam_skipped,
                                           "writer_errors": self.writer_errors,
                                           "late": self.n_late,
                                           **self._stream_counts(),
                                           "t_end": time.time()}) + "\n")
                self._fh.close()
            except (OSError, ValueError):
                pass
            for handle in (self._index_fh, self._streams_fh):
                if handle is not None:
                    try:
                        handle.close()
                    except (OSError, ValueError):
                        pass
            self._fh = None
            self._sid = None
            self._t0 = None
            self._frames_dir = None
            self._index_fh = None
            self._streams_fh = None

    def _ensure_frames_locked(self):
        """Create the sidecar directory while the recorder owns this session."""
        if self._frames_dir is None:
            self._frames_dir = os.path.join(self.dir, self._sid + ".frames")
            os.makedirs(self._frames_dir, exist_ok=True)
            self._index_fh = open(os.path.join(self._frames_dir, "index.jsonl"), "a")

    def _calibration_file_locked(self, cal):
        """The calibration file this depth frame refers to, written on first
        sight. ONE per session normally; a second (calibration-1.json, ...)
        only if the calibration itself changes -- a camera restart mid-session
        can bring a different serial or intrinsics, and silently pointing the
        old file at the new frames would put every later can position a few
        centimetres off with nothing saying why. Compared as dicts, so a
        byte-identical re-send costs nothing."""
        for known, fname in self._calibrations:
            if known == cal:
                return fname
        n = len(self._calibrations)
        fname = "calibration.json" if n == 0 else f"calibration-{n}.json"
        with open(os.path.join(self._frames_dir, fname), "w") as fh:
            fh.write(json.dumps(cal, indent=2, sort_keys=True) + "\n")
        self._calibrations.append((cal, fname))
        return fname

    def _write_camera_locked(self, t_recv, header, jpeg, depth=None):
        """Write one frame file, its depth beside it when there is one, and the
        index line.

        The historical name is kept for tests and old tooling, but the caller
        deliberately does *not* hold ``_lock`` during this disk work.  The
        writer is single-threaded and stop() waits for ``_inflight`` before it
        closes the session, so the file lifetime is already protected without
        making the 90 Hz note() path wait behind a slow JPEG write.
        """
        if self._frames_dir is None:
            d = os.path.join(self.dir, self._sid + ".frames")
            os.makedirs(d, exist_ok=True)
            self._frames_dir = d
            self._index_fh = open(os.path.join(d, "index.jsonl"), "a")
        seq = int(header.get("seq", self.n_cam_written + 1))
        name = f"{seq:06d}.jpg"
        with open(os.path.join(self._frames_dir, name), "wb") as fh:
            fh.write(jpeg)
        row = {"seq": seq, "t": header.get("t"),
               "t_recv": round(t_recv, 4), "bytes": len(jpeg),
               "source_frame_id": header.get("frame_id"),
               "captured_at_unix_ns": header.get("captured_at_unix_ns"),
               "received_at_unix_ns": header.get("received_at_unix_ns"),
               "received_at_monotonic_ns": header.get("received_at_monotonic_ns"),
               "depth_status": header.get("depth_status"),
               "w": header.get("w"), "h": header.get("h"),
               "file": name,
               # Per frame, null when no depth came: the truth about THIS
               # frame, never "the session has depth" on average.
               "depth_file": None, "depth_bytes": None, "depth_scale_m": None,
               "depth_captured_at_unix_ns": None, "calibration_file": None}
        if isinstance(depth, dict) and depth.get("depth_png"):
            dname = f"{seq:06d}.depth.png"
            with open(os.path.join(self._frames_dir, dname), "wb") as fh:
                fh.write(depth["depth_png"])
            cal = depth.get("calibration") or {}
            row.update(depth_file=dname, depth_bytes=len(depth["depth_png"]),
                       depth_scale_m=cal.get("depth_scale_m"),
                       depth_captured_at_unix_ns=depth.get("captured_at_unix_ns"),
                       calibration_file=self._calibration_file_locked(cal))
            self.n_depth_written += 1
        else:
            self.n_depth_missing += 1
        self._index_fh.write(json.dumps(row) + "\n")
        self.n_cam_written += 1

    def _finish_item(self):
        """Caller holds the lock: one item done, tell a waiting stop()."""
        self._inflight -= 1
        self._cv.notify_all()

    def _run(self):
        while not self._stop:
            self._wake.wait(0.2)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._q:
                        break
                    closing = self._closing
                    if closing is not None and time.monotonic() >= closing[1]:
                        break        # stop()'s deadline passed: let it have the lock
                    item = self._q.popleft()
                    self._inflight += 1
                    if item[0] == "artifact":
                        self._artifact_pending = {"sid": item[1], "bytes": len(item[3]), "accounted": False}
                try:
                    self._write_item(item)
                except Exception as e:  # noqa: BLE001 -- the writer thread must outlive any one item
                    # An escaped exception here used to end the thread, and
                    # with it every recording for the rest of the process.
                    # Count it, say so, carry on.
                    with self._lock:
                        self.writer_errors += 1
                    print(f"[recorder] writer error on {item[0]}: {e!r}")
                finally:
                    with self._lock:
                        self._finish_item()

    def _write_artifact(self, item):
        _, sid, event, data = item
        with self._lock:
            if self._sid != sid or self._fh is None:
                self.n_late += 1
                return
            pending = self._artifact_pending
            if pending["accounted"]:
                return
        # Write outside the input queue lock and outside the sealed directory.
        # A timed-out stop cancels publication, even if disk I/O finishes later.
        temporary = os.path.join(self.dir, f".{sid}-{event['sequence']}.partial")
        try:
            with open(temporary, "xb") as blob:
                blob.write(data)
            event["artifact"]["sha256"] = hashlib.sha256(data).hexdigest()
            line = json.dumps(event) + "\n"
            with self._lock:
                if pending["accounted"] or self._sid != sid or self._fh is None:
                    return
                self._ensure_frames_locked()
                os.replace(temporary, os.path.join(self._frames_dir, event["artifact"]["file"]))
                if self._streams_fh is None:
                    self._streams_fh = open(os.path.join(self._frames_dir, "streams.jsonl"), "a")
                self._streams_fh.write(line)
                self.n_stream_written += 1
                self.n_artifact_written += 1
                self._artifact_bytes -= len(data)
                pending["accounted"] = True
        except Exception:
            with self._lock:
                if not pending["accounted"] and self._sid == sid:
                    self.n_stream_dropped += 1
                    self.n_artifact_dropped += 1
                    self._artifact_bytes -= len(data)
                    pending["accounted"] = True
            raise
        finally:
            with self._lock:
                if self._artifact_pending is pending:
                    self._artifact_pending = None
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _write_item(self, item):
        kind, sid = item[0], item[1]
        if kind == "artifact":
            self._write_artifact(item)
            return
        if kind == "stream":
            with self._lock:
                if self._sid != sid or self._fh is None:
                    self.n_late += 1
                    return
                try:
                    self._ensure_frames_locked()
                    if self._streams_fh is None:
                        self._streams_fh = open(os.path.join(self._frames_dir, "streams.jsonl"), "a")
                    self._streams_fh.write(json.dumps(item[2]) + "\n")
                    self.n_stream_written += 1
                except (OSError, TypeError, ValueError):
                    self.n_stream_dropped += 1
                    raise
            return
        if kind == "cam":
            # Check ownership under the lock, then release it before touching
            # disk.  This item is already counted as `_inflight`, and stop()
            # waits for inflight work before closing the session, so the file
            # lifetime stays safe without stalling note() on camera I/O.
            with self._lock:
                if self._sid != sid or self._fh is None:
                    self.n_late += 1
                    return
                # Lock order is always queue -> I/O.  Acquire I/O while the
                # ownership check is still protected, then release the queue
                # lock for the whole disk operation.
                self._io_lock.acquire()
            failure = None
            try:
                self._write_camera_locked(*item[2:])
            except OSError as e:
                failure = e
            finally:
                self._io_lock.release()
            if failure is not None:
                print(f"[recorder] camera frame write failed: {failure}")
                with self._lock:
                    self.n_cam_dropped += 1
            return
        # Rows, marks and outcomes: take the handle under the lock only if
        # the item's session is still the open one, then write outside it
        # (a row is a few KB of JSON; the hot path must not wait on that).
        # stop() cannot close the handle underneath us: it waits for
        # _inflight to reach zero before it writes the footer.
        with self._lock:
            if self._sid != sid or self._fh is None:
                self.n_late += 1
                return
            fh = self._fh
        if kind == "mark":
            line = json.dumps({"type": "mark", "t": round(item[2], 4), "label": item[3]})
        elif kind == "outcome":
            line = json.dumps({"type": "outcome", "t": round(item[2], 4),
                               "result": item[3], "phase": item[4], "note": item[5]})
        else:
            line = json.dumps(self._row(item))
        try:
            fh.write(line + "\n")
        except (OSError, ValueError, TypeError) as e:
            with self._lock:
                if self._sid == sid and self._fh is not None:
                    # A real write failure on the OPEN session: end it, footer
                    # and all. Only this session -- the old path closed
                    # whatever was current, which could be the next one.
                    print(f"[recorder] write failed, stopping session: {e}")
                    self._close_locked()
                else:
                    self.n_late += 1
            return
        if kind == "row":
            with self._lock:
                if self._sid == sid:
                    self.n_written += 1

    @staticmethod
    def _row(item):
        (_, _sid, t, frame, targets, robot, ref, cam, sent, off, wrist_gain) = item
        row = {"t": round(t, 4),
               "hands": [dict({"hand": h.get("hand"),
                               "landmarks": h.get("landmarks"),
                               "wrist": h.get("wrist"),
                               "quat": h.get("quat"),
                               "fabricated": h.get("fabricated")},
                              # A controller instead of a tracked hand:
                              # which it was, and what the buttons said.
                              # The grip orientation is recorded RAW in
                              # `quat`; the bridge aligns it, so a
                              # replay reproduces and a recording can
                              # re-derive the alignment.
                              **{k: h[k] for k in ("source", "trigger", "squeeze",
                                                   "thumbstick", "buttons")
                                 if k in h})
                         for h in (frame.get("hands") or ())],
               "head": frame.get("head")}
        if robot:
            row["robot"] = robot
        if ref is not None:
            # Full precision on purpose: this is what makes a replay
            # reproduce the wrists to the digit, and it is written a
            # handful of times per session, not per frame.
            row["wrist_ref"] = ref
        if cam:
            # The camera frame that was current for this row: its seq
            # (a file in <sid>.frames/), its capture time on the robot's
            # clock, and how old it was here when the row was written.
            row["cam"] = cam
        if targets:
            # Rounded: these are radians, and 5 decimals is a thousandth
            # of a degree. Full float repr would nearly double the file
            # for precision no joint can express.
            row["targets"] = {k: round(float(v), 5)
                              for k, v in targets.items()
                              if isinstance(v, (int, float))}
        if sent:
            # What the robot was TOLD for this frame: the arm targets
            # after the arming offset. Rounded like `targets`.
            row["sent"] = {k: round(float(v), 5)
                           for k, v in sent.items()
                           if isinstance(v, (int, float))}
        if off is not None:
            # Full precision, once per capture: targets - sent, exactly.
            row["arm_offset"] = off
        if wrist_gain:
            # Versioned derived observation beside the raw WebXR wrist. Raw
            # input stays authoritative and can be reprocessed offline.
            row["controller_wrist_gain"] = wrist_gain
        return row

    def close(self):
        self._stop = True
        self._wake.set()
        with self._lock:
            if self._fh is not None:
                self._close_locked()
