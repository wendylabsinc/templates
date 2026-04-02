import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { spawn, type ChildProcess } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// AudioCapture — singleton that manages a GStreamer child process
// ---------------------------------------------------------------------------

class AudioCapture {
  private static instance: AudioCapture;

  private process: ChildProcess | null = null;
  private clients: Set<WebSocket> = new Set();

  private constructor() {}

  static getInstance(): AudioCapture {
    if (!AudioCapture.instance) {
      AudioCapture.instance = new AudioCapture();
    }
    return AudioCapture.instance;
  }

  // ---- client tracking ----------------------------------------------------

  addClient(ws: WebSocket): void {
    this.clients.add(ws);
    // Start pipeline on first client
    if (this.clients.size === 1 && !this.process) {
      this.startPipeline();
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

  private startPipeline(): void {
    this.stopPipeline();

    console.log("[gst] starting audio capture pipeline");

    this.process = spawn("gst-launch-1.0", [
      "autoaudiosrc",
      "!",
      "audioconvert",
      "!",
      "audio/x-raw,format=S16LE,channels=1,rate=16000",
      "!",
      "fdsink",
      "fd=1",
    ]);

    this.process.stdout?.on("data", (chunk: Buffer) => {
      this.broadcastChunk(chunk);
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
  }

  // ---- broadcast ----------------------------------------------------------

  private broadcastChunk(chunk: Buffer): void {
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(chunk);
      }
    }
  }

  shutdown(): void {
    this.stopPipeline();
    this.clients.clear();
  }
}

// ---------------------------------------------------------------------------
// Express + WebSocket server
// ---------------------------------------------------------------------------

const audio = AudioCapture.getInstance();

const app = express();
const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/stream" });

// ---- WebSocket ------------------------------------------------------------

wss.on("connection", (ws) => {
  console.log("[ws] client connected");
  audio.addClient(ws);

  ws.on("message", (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.play === "string") {
        console.log(`[ws] play request: ${data.play}`);
      }
    } catch {
      // ignore non-JSON messages
    }
  });

  ws.on("close", () => {
    console.log("[ws] client disconnected");
    audio.removeClient(ws);
  });
});

// ---- HTTP routes ----------------------------------------------------------

const assetsDir = path.resolve(__dirname, "..", "assets");

app.get("/", (_req, res) => {
  const htmlPath = path.resolve(__dirname, "..", "index.html");
  res.sendFile(htmlPath);
});

app.use("/assets", express.static(assetsDir));

app.get("/sounds", (_req, res) => {
  try {
    const files = fs.readdirSync(assetsDir).filter((f) =>
      f.toLowerCase().endsWith(".wav")
    );
    const sounds = files.map((f) => ({
      name: path.basename(f, ".wav"),
      file: f,
    }));
    res.json(sounds);
  } catch {
    res.json([]);
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

server.listen(PORT, () => {
  console.log(
    `Audio server listening on http://${WENDY_HOSTNAME}:${PORT}`
  );
});

process.on("SIGINT", () => {
  audio.shutdown();
  process.exit(0);
});

process.on("SIGTERM", () => {
  audio.shutdown();
  process.exit(0);
});
