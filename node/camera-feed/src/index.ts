import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { spawn, execSync, type ChildProcess } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// MJPEGCamera — singleton that manages a GStreamer child process
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

  addClient(ws: WebSocket): void {
    this.clients.add(ws);
    // Start pipeline on first client
    if (this.clients.size === 1 && !this.process) {
      this.startPipeline(this.device);
    }
  }

  removeClient(ws: WebSocket): void {
    this.clients.delete(ws);
    // Kill pipeline when last client disconnects
    if (this.clients.size === 0) {
      this.stopPipeline();
    }
  }

  // ---- pipeline management ------------------------------------------------

  switchCamera(device: string): void {
    this.stopPipeline();
    this.startPipeline(device);
  }

  private startPipeline(device: string): void {
    this.stopPipeline();
    this.device = device;
    this.buffer = Buffer.alloc(0);

    console.log(`[gst] starting pipeline for ${device}`);

    this.process = spawn("gst-launch-1.0", [
      "v4l2src",
      `device=${device}`,
      "!",
      "image/jpeg",
      "!",
      "fdsink",
      "fd=1",
    ]);

    this.process.stdout?.on("data", (chunk: Buffer) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this.extractFrames();
    });

    this.process.stderr?.on("data", (data: Buffer) => {
      console.error(`[gst] ${data.toString()}`);
    });

    this.process.on("close", (code) => {
      console.log(`[gst] process exited with code ${code}`);
      this.process = null;
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

  private extractFrames(): void {
    while (true) {
      const start = this.findMarker(0xff, 0xd8);
      if (start === -1) {
        // No start marker -- discard everything
        this.buffer = Buffer.alloc(0);
        break;
      }

      // Drop bytes before the start marker
      if (start > 0) {
        this.buffer = this.buffer.subarray(start);
      }

      const end = this.findMarker(0xff, 0xd9, 2);
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

  private findMarker(b0: number, b1: number, offset = 0): number {
    for (let i = offset; i < this.buffer.length - 1; i++) {
      if (this.buffer[i] === b0 && this.buffer[i + 1] === b1) {
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

wss.on("connection", (ws) => {
  console.log("[ws] client connected");
  camera.addClient(ws);

  ws.on("message", (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.switch_camera === "string") {
        console.log(`[ws] switching camera to ${data.switch_camera}`);
        camera.switchCamera(data.switch_camera);
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
    const output = execSync("v4l2-ctl --list-devices", {
      encoding: "utf-8",
      timeout: 5000,
    });

    const cameras: { id: string; name: string }[] = [];
    let currentName = "";

    for (const line of output.split("\n")) {
      if (line.length === 0) continue;

      if (!line.startsWith("\t")) {
        currentName = line.replace(/:$/, "").trim();
      } else {
        const devPath = line.trim();
        if (devPath.startsWith("/dev/video")) {
          cameras.push({ id: devPath, name: currentName });
        }
      }
    }

    res.json(cameras);
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
    `Camera feed server listening on http://${WENDY_HOSTNAME}:${PORT}`
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
