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

// `arecord -l` / `aplay -l` lines look like:
//   card 0: PCH [HDA Intel PCH], device 3: HDMI 0 [HDMI 0]
// HDMI outputs commonly use device 3, 7, etc., so we must capture the
// device number alongside the card number and dedupe on the pair.
const ALSA_DEVICE_LINE =
  /^card\s+(\d+):\s*([^[]*?)\s*(?:\[[^\]]*\])?\s*,\s*device\s+(\d+):\s*([^[]*?)\s*(?:\[|$)/;

function parseAudioDevices(command: "arecord" | "aplay"): AudioDevice[] {
  try {
    const output = execFileSync(command, ["-l"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 2000,
    });

    const seen = new Set<string>();
    const devices: AudioDevice[] = [];
    for (const line of output.split("\n")) {
      const match = ALSA_DEVICE_LINE.exec(line);
      if (!match) continue;
      const [, cardNum, cardName, deviceNum, deviceName] = match;

      const id = `hw:${cardNum},${deviceNum}`;
      if (seen.has(id)) continue;
      seen.add(id);

      const card = cardName.trim();
      const device = deviceName.trim();
      const name =
        card && device
          ? `${card} - ${device}`
          : device || card || `Card ${cardNum} device ${deviceNum}`;
      devices.push({ id, name });
    }
    return devices;
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
  private leftover: Buffer = Buffer.alloc(0);

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

    const child = spawn("gst-launch-1.0", [
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
    this.process = child;

    child.stdout?.on("data", (chunk: Buffer) => {
      // S16LE samples are 2 bytes — only forward aligned slices and carry the
      // trailing odd byte (if any) into the next chunk. Otherwise the browser
      // throws "byte length of Int16Array should be a multiple of 2".
      const combined = this.leftover.length
        ? Buffer.concat([this.leftover, chunk])
        : chunk;
      const aligned = combined.length - (combined.length % 2);
      this.leftover = combined.subarray(aligned);
      if (aligned > 0) {
        this.broadcastChunk(combined.subarray(0, aligned));
      }
    });

    child.stderr?.on("data", (data: Buffer) => {
      console.error(`[gst] ${data.toString()}`);
    });

    child.on("close", (code) => {
      console.log(`[gst] process exited with code ${code}`);
      // Only clear `this.process` if it still references the child whose
      // close we're handling. Otherwise a switchMicrophone() that races with
      // an in-flight close would null out the freshly-spawned pipeline and
      // leak the new process.
      if (this.process === child) {
        this.process = null;
      }
    });
  }

  private stopPipeline(): void {
    if (this.process) {
      this.process.kill("SIGTERM");
      this.process = null;
    }
    this.leftover = Buffer.alloc(0);
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

  // Send only while the socket is open, so a client that disconnects mid-switch
  // can't make ws.send throw and take down the message handler.
  const safeSend = (payload: object) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  };

  ws.on("message", (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.switch_microphone === "string") {
        try {
          audio.switchMicrophone(data.switch_microphone);
          // Acknowledge so the UI can leave the "Switching" state.
          safeSend({ type: "mic_switched", device: data.switch_microphone });
        } catch {
          safeSend({ type: "mic_switch_failed" });
        }
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
