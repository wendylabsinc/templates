import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { spawn, execFileSync, type ChildProcess } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// V4L2 helpers -- device capability / naming lookups via v4l2-ctl.
// Each invocation is wrapped in `timeout 2` (coreutils) so a wedged v4l2-ctl
// call can't hang an HTTP request. Mirrors cpp/webcam and rust/webcam's
// enumerate_cameras()/v4l2_is_capture()/v4l2_device_name() helpers, which in
// turn mirror python/webcam/app.py's enumerate_cameras().
// ---------------------------------------------------------------------------

interface CameraInfo {
  id: string;
  name: string;
}

function runV4l2(args: string[], timeoutSeconds = 2): string {
  try {
    return execFileSync("timeout", [`${timeoutSeconds}`, "v4l2-ctl", ...args], {
      encoding: "utf-8",
    });
  } catch {
    return "";
  }
}

/** Returns true if the given /dev/videoN node is a capture-capable device
 * (as opposed to a metadata-only or output-only node some UVC cameras
 * expose alongside their capture node). */
function v4l2IsCapture(devicePath: string): boolean {
  const output = runV4l2(["--device", devicePath, "--all"]);
  return output.includes("Video Capture");
}

/** Reads the human-readable "Card type" for a /dev/videoN node, falling
 * back to the device basename if v4l2-ctl is unavailable or the field is
 * missing. */
function v4l2DeviceName(devicePath: string): string {
  const output = runV4l2(["--device", devicePath, "--info"]);
  for (const line of output.split("\n")) {
    const idx = line.indexOf("Card type");
    if (idx === -1) continue;
    const colon = line.indexOf(":", idx);
    if (colon === -1) continue;
    return line.slice(colon + 1).trim();
  }
  return path.basename(devicePath);
}

/** Fallback enumeration used when `v4l2-ctl --list-devices` yields nothing
 * (e.g. udev naming quirks). Scans /dev/video* directly. */
function enumerateCamerasFallback(): CameraInfo[] {
  let entries: string[] = [];
  try {
    entries = fs
      .readdirSync("/dev")
      .filter((f) => f.startsWith("video"))
      .map((f) => `/dev/${f}`)
      .sort();
  } catch {
    return [];
  }
  return entries
    .filter((p) => v4l2IsCapture(p))
    .map((p) => ({ id: p, name: v4l2DeviceName(p) }));
}

/** Enumerate capture-capable V4L2 devices as [{ id, name }, ...]. Groups by
 * `v4l2-ctl --list-devices`, keeps only capture-capable nodes, and falls
 * back to scanning /dev/video* directly if that yields nothing. */
function enumerateCameras(): CameraInfo[] {
  const output = runV4l2(["--list-devices"]);
  const cameras: CameraInfo[] = [];
  let currentName = "";

  for (const line of output.split("\n")) {
    if (line.length === 0) continue;

    if (!line.startsWith("\t") && !line.startsWith(" ")) {
      currentName = line.replace(/:$/, "").trim();
    } else {
      const devPath = line.trim();
      if (devPath.startsWith("/dev/video") && v4l2IsCapture(devPath)) {
        cameras.push({ id: devPath, name: currentName || devPath });
      }
    }
  }

  if (cameras.length === 0) {
    return enumerateCamerasFallback();
  }
  return cameras;
}

// ---------------------------------------------------------------------------
// MJPEGCamera -- singleton that manages a GStreamer child process
// ---------------------------------------------------------------------------

class MJPEGCamera {
  private static instance: MJPEGCamera;

  private process: ChildProcess | null = null;
  private device: string = "/dev/video0";
  private clients: Set<WebSocket> = new Set();
  private buffer: Buffer = Buffer.alloc(0);

  private constructor() {}

  static getInstance(): MJPEGCamera {
    if (!MJPEGCamera.instance) {
      MJPEGCamera.instance = new MJPEGCamera();
    }
    return MJPEGCamera.instance;
  }

  // ---- client tracking ----------------------------------------------------

  async addClient(ws: WebSocket): Promise<boolean> {
    this.clients.add(ws);
    // Start pipeline on first client
    if (this.clients.size === 1 && !this.process) {
      const ok = await this.startPipeline(this.device);
      if (!ok) {
        this.clients.delete(ws);
        return false;
      }
    }
    return true;
  }

  removeClient(ws: WebSocket): void {
    this.clients.delete(ws);
    // Kill pipeline when last client disconnects
    if (this.clients.size === 0) {
      this.stopPipeline();
    }
  }

  // ---- pipeline management --------------------------------------------------

  async switchCamera(device: string): Promise<boolean> {
    this.stopPipeline();
    return this.startPipeline(device);
  }

  /** Candidate pipeline ladder, most permissive first -- mirrors
   * python/webcam/app.py's MJPEGCamera._start_pipeline:
   *  1. native MJPEG passthrough (most USB webcams emit MJPEG already, so
   *     no transcoding is needed)
   *  2. native MJPEG constrained to 640x480 (helps devices that only
   *     negotiate MJPEG at fixed resolutions)
   *  3. raw video re-encoded to JPEG in software (works for any v4l2
   *     source, at the cost of CPU)
   * Frames are read from stdout via fdsink -- the CLI-process analogue of
   * the appsink used by python/cpp/rust's in-process GStreamer bindings. */
  private buildCandidates(device: string): string[][] {
    return [
      ["v4l2src", `device=${device}`, "!", "image/jpeg", "!", "fdsink", "fd=1"],
      [
        "v4l2src",
        `device=${device}`,
        "!",
        "image/jpeg,width=640,height=480",
        "!",
        "fdsink",
        "fd=1",
      ],
      [
        "v4l2src",
        `device=${device}`,
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        "quality=70",
        "!",
        "fdsink",
        "fd=1",
      ],
    ];
  }

  private async startPipeline(device: string): Promise<boolean> {
    this.stopPipeline();
    this.device = device;

    for (const args of this.buildCandidates(device)) {
      const ok = await this.tryPipeline(args);
      if (ok) {
        console.log(`[gst] pipeline ready for ${device}: ${args.join(" ")}`);
        return true;
      }
    }

    console.error(`[gst] no working pipeline found for ${device}`);
    return false;
  }

  /** Spawns a single pipeline candidate and "prerolls" it by waiting for a
   * complete JPEG frame (or a quick failure) before committing to it --
   * the CLI-process analogue of the PAUSED-state preroll used by
   * python/cpp/rust's in-process GStreamer bindings. */
  private tryPipeline(args: string[]): Promise<boolean> {
    return new Promise((resolve) => {
      let settled = false;
      let probeBuffer = Buffer.alloc(0);

      const proc = spawn("gst-launch-1.0", args);

      const timer = setTimeout(() => fail(), 3000);

      const succeed = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.process = proc;
        this.buffer = probeBuffer;
        this.attachStreamingHandlers(proc);
        this.extractFrames();
        resolve(true);
      };

      const fail = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        proc.removeAllListeners();
        try {
          proc.kill("SIGTERM");
        } catch {
          // already exited
        }
        resolve(false);
      };

      proc.stdout?.on("data", (chunk: Buffer) => {
        probeBuffer = Buffer.concat([probeBuffer, chunk]);
        if (this.hasCompleteFrame(probeBuffer)) {
          succeed();
        }
      });

      proc.stderr?.on("data", (data: Buffer) => {
        console.error(`[gst] ${data.toString()}`);
      });

      proc.on("error", () => fail());
      proc.on("close", (code) => {
        if (!settled) {
          console.log(`[gst] pipeline candidate exited (code ${code}) before producing a frame`);
          fail();
        }
      });
    });
  }

  private attachStreamingHandlers(proc: ChildProcess): void {
    proc.stdout?.on("data", (chunk: Buffer) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this.extractFrames();
    });

    proc.stderr?.on("data", (data: Buffer) => {
      console.error(`[gst] ${data.toString()}`);
    });

    proc.on("close", (code) => {
      console.log(`[gst] process exited with code ${code}`);
      if (this.process === proc) {
        this.process = null;
      }
    });
  }

  private stopPipeline(): void {
    if (this.process) {
      this.process.kill("SIGTERM");
      this.process = null;
    }
    this.buffer = Buffer.alloc(0);
  }

  // ---- JPEG frame extraction ----------------------------------------------

  private hasCompleteFrame(buf: Buffer): boolean {
    const start = this.findMarker(buf, 0xff, 0xd8);
    if (start === -1) return false;
    return this.findMarker(buf, 0xff, 0xd9, start + 2) !== -1;
  }

  private extractFrames(): void {
    while (true) {
      const start = this.findMarker(this.buffer, 0xff, 0xd8);
      if (start === -1) {
        // No start marker -- discard everything
        this.buffer = Buffer.alloc(0);
        break;
      }

      // Drop bytes before the start marker
      if (start > 0) {
        this.buffer = this.buffer.subarray(start);
      }

      const end = this.findMarker(this.buffer, 0xff, 0xd9, 2);
      if (end === -1) {
        // Incomplete frame -- wait for more data
        break;
      }

      const frameEnd = end + 2; // include the FFD9 marker
      const frame = this.buffer.subarray(0, frameEnd);
      this.buffer = this.buffer.subarray(frameEnd);

      this.broadcastFrame(frame);
    }
  }

  private findMarker(buf: Buffer, b0: number, b1: number, offset = 0): number {
    for (let i = offset; i < buf.length - 1; i++) {
      if (buf[i] === b0 && buf[i + 1] === b1) {
        return i;
      }
    }
    return -1;
  }

  private broadcastFrame(frame: Buffer): void {
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(frame);
      }
    }
  }

  get currentDevice(): string {
    return this.device;
  }

  shutdown(): void {
    this.stopPipeline();
    this.clients.clear();
  }
}

// ---------------------------------------------------------------------------
// Express + WebSocket server
// ---------------------------------------------------------------------------

const camera = MJPEGCamera.getInstance();

const app = express();
const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/stream" });

// ---- WebSocket ------------------------------------------------------------

wss.on("connection", async (ws) => {
  console.log("[ws] client connected");
  const ok = await camera.addClient(ws);
  if (!ok) {
    console.error("[ws] failed to start camera pipeline");
    ws.close(1011, "camera unavailable");
    return;
  }

  ws.on("message", async (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.switch_camera === "string") {
        console.log(`[ws] switching camera to ${data.switch_camera}`);
        const switched = await camera.switchCamera(data.switch_camera);
        if (!switched) {
          console.error(`[ws] camera switch failed: ${data.switch_camera}`);
        }
      }
    } catch {
      // ignore non-JSON messages
    }
  });

  ws.on("close", () => {
    console.log("[ws] client disconnected");
    camera.removeClient(ws);
  });
});

// ---- HTTP routes ----------------------------------------------------------

app.get("/", (_req, res) => {
  const htmlPath = path.resolve(__dirname, "..", "index.html");
  res.sendFile(htmlPath);
});

app.use("/assets", express.static(path.resolve(__dirname, "..", "assets")));

app.get("/cameras", (_req, res) => {
  try {
    res.json(enumerateCameras());
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    res.status(500).json({ error: "Failed to list cameras", details: message });
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

server.listen(PORT, () => {
  console.log(
    `Webcam server listening on http://${WENDY_HOSTNAME}:${PORT}`
  );
});

process.on("SIGINT", () => {
  camera.shutdown();
  process.exit(0);
});

process.on("SIGTERM", () => {
  camera.shutdown();
  process.exit(0);
});
