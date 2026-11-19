"""Bounded control evidence; no raw poses, credentials, or network payloads."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import secrets
import sys
import threading
import time

_FIELDS = set('command request_id message state previous reason source phase commit '
              'latency_ms previous_duration_s clients visibility xr_visibility tracked_inputs '
              'left_stick right_stick left_grip right_grip configured held fresh button '
              'release_required applied refused error age_s'.split())


class ControlEvents:
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time, capacity=200, path=None):
        self.clock, self.wall_clock = clock, wall_clock
        self.session_id = secrets.token_hex(6)
        self.started_at = wall_clock()
        self.events = deque(maxlen=capacity)
        self.pending = {}
        self.transitions = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.last_command = None
        self.dropped = self.write_errors = 0
        self.path = str(path) if path else None
        self.queue = queue.Queue(maxsize=512)
        self.stop = threading.Event()
        self.worker = None
        if path:
            self.worker = threading.Thread(target=self._write, daemon=True, name='control-events')
            self.worker.start()

    def emit(self, event, **fields):
        with self.lock:
            now = self.wall_clock()
            item = {'session_id':self.session_id, 'at':now,
                    'at_utc':datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),
                    'event':str(event)[:80]}
            for key, value in fields.items():
                if key in _FIELDS and (value is None or type(value) in (bool,int,float,str)) and not (type(value) is float and not math.isfinite(value)):
                    item[key] = value[:500] if isinstance(value,str) else value
            self.events.append(item)
            if self.worker:
                try: self.queue.put_nowait(item)
                except queue.Full: self.dropped += 1
            return item

    def transition(self, key, value, event, **fields):
        with self.lock:
            old = self.transitions.get(key)
            if old is not None and old[0] == value:
                return
            now = self.clock()
            self.transitions[key] = (value, now)
            self.emit(event, previous_duration_s=round(now-old[1],3) if old else None, **fields)

    def sent(self, command, request_id=None):
        with self.lock:
            self.counter += 1
            request_id = request_id or f'{self.session_id}-{self.counter}'
            if len(self.pending) >= 64:
                oldest = next(iter(self.pending))
                old = self.pending.pop(oldest)
                self.emit('command.unconfirmed', request_id=oldest, command=old[0], reason='pending history limit')
            self.pending[request_id] = (command, self.clock())
            self.emit('command.send_attempted', command=command, request_id=request_id)
            return request_id

    def reply(self, request_id, command, message, state):
        with self.lock:
            request_id = request_id if isinstance(request_id, str) else None
            pending = self.pending.pop(request_id, None)
            latency = round((self.clock()-pending[1])*1000,1) if pending else None
            self.last_command = dict(self.emit('command.reply', request_id=request_id,
                command=command, message=message, state=state, latency_ms=latency), received=self.clock())

    def disconnected(self):
        with self.lock:
            for request_id, (command, _) in self.pending.items():
                self.emit('command.unconfirmed', request_id=request_id, command=command, reason='robot connection lost; outcome unknown')
            self.pending.clear()

    def snapshot(self):
        with self.lock:
            now = self.clock()
            last = dict(self.last_command) if self.last_command else None
            if last: last['age_s'] = round(now-last.pop('received'),3)
            return {'session_id':self.session_id, 'started_at':self.started_at,
                    'events':list(self.events), 'last_command':last,
                    'pending_commands':[{'request_id':rid,'command':cmd,'age_s':round(now-at,3),
                        'overdue':now-at>3.} for rid,(cmd,at) in self.pending.items()],
                    'dropped':self.dropped,'write_errors':self.write_errors,'log_file':self.path}

    def _write(self):
        handlers = []
        try:
            path = Path(self.path);path.parent.mkdir(parents=True, exist_ok=True)
            owner = self
            class EventFileHandler(RotatingFileHandler):
                def handleError(self, record):
                    owner.write_errors += 1
            handler = EventFileHandler(path,maxBytes=5_000_000,backupCount=3,encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'));handlers.append(handler)
            handlers.append(logging.StreamHandler(sys.stdout))
            while not self.stop.is_set() or not self.queue.empty():
                try: item = self.queue.get(timeout=.1)
                except queue.Empty: continue
                try:
                    raw = json.dumps(item,allow_nan=False,separators=(',',':'))
                    record = logging.LogRecord('control',logging.INFO,'',0,raw,(),None)
                    for h in handlers: h.handle(record)
                except Exception: self.write_errors += 1
        except Exception:
            self.write_errors += 1
        finally:
            for h in handlers: h.close()

    def close(self):
        self.stop.set()
        if self.worker: self.worker.join(timeout=2.)
