import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { execFileSync, spawn, type ChildProcess } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const assetsDir = path.resolve(__dirname, "..", "assets");

type AudioDevice = {
  id: string;
  name: string;
};

function parseAudioDevices(command: "arecord" | "aplay"): AudioDevice[] {
  try {
    const output = execFileSync(command, ["-l"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 2000,
    });

    return output
      .split("\n")
      .filter((line) => line.startsWith("card "))
      .flatMap((line) => {
        const [cardPart, rest = ""] = line.split(":", 2);
        const cardNum = cardPart.split(/\s+/)[1]?.replace(/:$/, "");
        if (!cardNum) return [];

        const name = rest.trim().split("[")[0]?.trim() || `Card ${cardNum}`;
        return [{ id: `hw:${cardNum},0`, name }];
      });
  } catch {
    return [];
  }
}

function displayName(file: string): string {
  return path
    .basename(file, ".wav")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function listSounds(): Array<{ name: string; file: string }> {
  try {
    return fs
      .readdirSync(assetsDir)
      .filter((file) => file.toLowerCase().endsWith(".wav"))
      .sort()
      .map((file) => ({
        name: displayName(file),
        file,
      }));
  } catch {
    return [];
  }
}

function resolveSoundPath(filename: string): string | null {
  if (
    filename !== path.basename(filename) ||
    !filename.toLowerCase().endsWith(".wav")
  ) {
    return null;
  }

  const filepath = path.resolve(assetsDir, filename);
  if (!filepath.startsWith(`${assetsDir}${path.sep}`) || !fs.existsSync(filepath)) {
    return null;
  }

  return filepath;
}

// ---------------------------------------------------------------------------
// AudioCapture — singleton that manages a GStreamer child process
// ---------------------------------------------------------------------------

class AudioCapture {
  private static instance: AudioCapture;

  private process: ChildProcess | null = null;
  private clients: Set<WebSocket> = new Set();
  private currentDevice: string | null = null;

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

    const source = this.currentDevice
      ? ["alsasrc", `device=${this.currentDevice}`]
      : ["autoaudiosrc"];

    this.process = spawn("gst-launch-1.0", [
      ...source,
      "!",
      "audioconvert",
      "!",
      "audioresample",
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

  switchMicrophone(deviceId: string): void {
    this.currentDevice = deviceId;
    if (this.clients.size > 0) {
      this.startPipeline();
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
let currentSpeaker: string | null = null;

// ---- WebSocket ------------------------------------------------------------

wss.on("connection", (ws) => {
  console.log("[ws] client connected");
  audio.addClient(ws);

  ws.on("message", (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.switch_microphone === "string") {
        audio.switchMicrophone(data.switch_microphone);
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

app.get("/", (_req, res) => {
  const htmlPath = path.resolve(__dirname, "..", "index.html");
  res.sendFile(htmlPath);
});

app.use("/assets", express.static(assetsDir));

app.get("/sounds", (_req, res) => {
  res.json(listSounds());
});

app.get("/microphones", (_req, res) => {
  res.json(parseAudioDevices("arecord"));
});

app.get("/speakers", (_req, res) => {
  res.json(parseAudioDevices("aplay"));
});

app.post("/speaker/:deviceId", (req, res) => {
  const deviceId = req.params.deviceId;
  if (!deviceId) {
    res.status(400).json({ error: "missing speaker" });
    return;
  }

  currentSpeaker = deviceId;
  res.json({ status: "ok", speaker: deviceId });
});

app.post("/play/:filename", (req, res) => {
  const filename = req.params.filename;
  const filepath = filename ? resolveSoundPath(filename) : null;
  if (!filename || !filepath) {
    res.status(404).json({ error: "not found" });
    return;
  }

  const sink = currentSpeaker
    ? ["alsasink", `device=${currentSpeaker}`]
    : ["autoaudiosink"];
  const child = spawn(
    "gst-launch-1.0",
    [
      "filesrc",
      `location=${filepath}`,
      "!",
      "wavparse",
      "!",
      "audioconvert",
      "!",
      "audioresample",
      "!",
      ...sink,
    ],
    { stdio: "ignore", detached: true }
  );
  child.unref();

  res.json({ status: "playing", file: filename });
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
