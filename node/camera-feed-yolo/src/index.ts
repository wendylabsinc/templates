import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { spawn, execSync, type ChildProcess } from "child_process";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import * as ort from "onnxruntime-node";
import sharp from "sharp";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const APP_ROOT = path.resolve(__dirname, "..");
const MODEL_PATH = path.resolve(APP_ROOT, "yolov8n.onnx");

const COCO_NAMES = [
  "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
  "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog",
  "horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella",
  "handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite",
  "baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle",
  "wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich",
  "orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch",
  "potted plant","bed","dining table","toilet","tv","laptop","mouse","remote",
  "keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book",
  "clock","vase","scissors","teddy bear","hair drier","toothbrush",
] as const;

const INPUT_SIZE = 640;

function envTruthy(name: string): boolean {
  const v = (process.env[name] ?? "").toLowerCase();
  return v === "true" || v === "1" || v === "yes";
}

function isRpi(): boolean {
  const dev = process.env.WENDY_DEVICE_TYPE ?? "";
  if (dev.startsWith("raspberrypi")) return true;
  if (dev) return false;
  try {
    return fs.readFileSync("/proc/device-tree/model", "utf8").includes("Raspberry Pi");
  } catch {
    return false;
  }
}

const USE_GPU = envTruthy("WENDY_HAS_GPU");
const IS_RPI = isRpi();
const USE_PASSTHROUGH = !USE_GPU || IS_RPI;
const MIN_INTERVAL_MS = 1000 / (USE_GPU ? 15 : 3);

console.log(
  `[startup] platform=${process.env.WENDY_PLATFORM ?? "unknown"}, has_gpu=${USE_GPU}, is_rpi=${IS_RPI}, capture=${USE_PASSTHROUGH ? "passthrough" : "decode-encode"}`,
);

// ---------------------------------------------------------------------------
// MJPEGCamera — spawns gst-launch-1.0 and parses framed JPEGs from stdout.
// ---------------------------------------------------------------------------

type FrameListener = (frame: Buffer) => void;

class MJPEGCamera {
  private process: ChildProcess | null = null;
  private device = "/dev/video0";
  private clients: Set<WebSocket> = new Set();
  private buffer: Buffer = Buffer.alloc(0);
  private listeners: Set<FrameListener> = new Set();

  addClient(ws: WebSocket): void {
    this.clients.add(ws);
    if (this.clients.size === 1 && !this.process) this.startPipeline(this.device);
  }

  removeClient(ws: WebSocket): void {
    this.clients.delete(ws);
    if (this.clients.size === 0) this.stopPipeline();
  }

  switchCamera(device: string): void {
    this.stopPipeline();
    this.startPipeline(device);
  }

  onFrame(cb: FrameListener): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  private startPipeline(device: string): void {
    this.stopPipeline();
    this.device = device;
    this.buffer = Buffer.alloc(0);

    // Passthrough on RPi/CPU avoids the decode/encode brown-out path; Jetson
    // re-encodes through HW JPEG codecs for consistent quality.
    const args = USE_PASSTHROUGH
      ? ["v4l2src", `device=${device}`, "!", "image/jpeg", "!", "fdsink", "fd=1"]
      : ["v4l2src", `device=${device}`, "!", "image/jpeg", "!", "jpegdec", "!", "jpegenc", "quality=85", "!", "fdsink", "fd=1"];

    console.log(`[gst] starting pipeline for ${device} (passthrough=${USE_PASSTHROUGH})`);

    this.process = spawn("gst-launch-1.0", args);
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

  private extractFrames(): void {
    while (true) {
      const start = this.findMarker(0xff, 0xd8);
      if (start === -1) {
        this.buffer = Buffer.alloc(0);
        break;
      }
      if (start > 0) this.buffer = this.buffer.subarray(start);
      const end = this.findMarker(0xff, 0xd9, 2);
      if (end === -1) break;
      const frameEnd = end + 2;
      const frame = this.buffer.subarray(0, frameEnd);
      this.buffer = this.buffer.subarray(frameEnd);
      const owned = Buffer.from(frame);
      for (const listener of this.listeners) {
        try { listener(owned); } catch (e) { console.error("[frame listener]", e); }
      }
    }
  }

  private findMarker(b0: number, b1: number, offset = 0): number {
    for (let i = offset; i < this.buffer.length - 1; i++) {
      if (this.buffer[i] === b0 && this.buffer[i + 1] === b1) return i;
    }
    return -1;
  }

  get clientList(): Set<WebSocket> {
    return this.clients;
  }

  shutdown(): void {
    this.stopPipeline();
    this.clients.clear();
  }
}

// ---------------------------------------------------------------------------
// YOLO inference engine
// ---------------------------------------------------------------------------

interface Box {
  x1: number; y1: number; x2: number; y2: number;
  conf: number; cls: number; name: string;
}

interface Meta {
  detections: number;
  inference_ms: number;
  classes: Record<string, number>;
  boxes: Box[];
  frame_w: number;
  frame_h: number;
}

const EMPTY_META: Meta = { detections: 0, inference_ms: 0, classes: {}, boxes: [], frame_w: 0, frame_h: 0 };

function iou(a: Box, b: Box): number {
  const x1 = Math.max(a.x1, b.x1);
  const y1 = Math.max(a.y1, b.y1);
  const x2 = Math.min(a.x2, b.x2);
  const y2 = Math.min(a.y2, b.y2);
  const w = Math.max(0, x2 - x1);
  const h = Math.max(0, y2 - y1);
  const inter = w * h;
  const aa = Math.max(0, a.x2 - a.x1) * Math.max(0, a.y2 - a.y1);
  const bb = Math.max(0, b.x2 - b.x1) * Math.max(0, b.y2 - b.y1);
  const u = aa + bb - inter;
  return u > 0 ? inter / u : 0;
}

class YoloEngine {
  private session: ort.InferenceSession | null = null;
  private inputName = "images";
  private outputName = "output0";

  async load(): Promise<void> {
    const providers = USE_GPU ? ["cuda", "cpu"] : ["cpu"];
    console.log(`[yolo] loading model (providers=${providers.join(",")})`);
    this.session = await ort.InferenceSession.create(MODEL_PATH, {
      executionProviders: providers,
      graphOptimizationLevel: "all",
    });
    if (this.session.inputNames.length) this.inputName = this.session.inputNames[0];
    if (this.session.outputNames.length) this.outputName = this.session.outputNames[0];
  }

  async infer(jpeg: Buffer, confThreshold: number): Promise<{ boxes: Box[]; w: number; h: number }> {
    if (!this.session) return { boxes: [], w: 0, h: 0 };

    // Read original size, then letterbox to INPUT_SIZE x INPUT_SIZE.
    const meta = await sharp(jpeg).metadata();
    const w = meta.width ?? 0;
    const h = meta.height ?? 0;
    if (!w || !h) return { boxes: [], w: 0, h: 0 };

    const scale = Math.min(INPUT_SIZE / w, INPUT_SIZE / h);
    const newW = Math.round(w * scale);
    const newH = Math.round(h * scale);
    const padX = Math.floor((INPUT_SIZE - newW) / 2);
    const padY = Math.floor((INPUT_SIZE - newH) / 2);

    const padded = await sharp(jpeg)
      .resize(newW, newH, { fit: "fill", kernel: "nearest" })
      .extend({
        top: padY,
        bottom: INPUT_SIZE - newH - padY,
        left: padX,
        right: INPUT_SIZE - newW - padX,
        background: { r: 114, g: 114, b: 114 },
      })
      .removeAlpha()
      .raw()
      .toBuffer();

    // HWC u8 -> CHW f32 [0,1].
    const plane = INPUT_SIZE * INPUT_SIZE;
    const tensor = new Float32Array(3 * plane);
    for (let i = 0; i < plane; i++) {
      const o = i * 3;
      tensor[i] = padded[o] / 255;
      tensor[plane + i] = padded[o + 1] / 255;
      tensor[2 * plane + i] = padded[o + 2] / 255;
    }
    const input = new ort.Tensor("float32", tensor, [1, 3, INPUT_SIZE, INPUT_SIZE]);
    const result = await this.session.run({ [this.inputName]: input });
    const output = result[this.outputName];
    const data = output.data as Float32Array;
    const dims = output.dims; // expect [1, 84, N]
    if (dims.length !== 3 || dims[1] < 84) return { boxes: [], w, h };
    const numAnchors = dims[2];

    const candidates: Box[] = [];
    for (let i = 0; i < numAnchors; i++) {
      let bestCls = 0;
      let bestScore = 0;
      for (let c = 0; c < 80; c++) {
        const s = data[(4 + c) * numAnchors + i];
        if (s > bestScore) { bestScore = s; bestCls = c; }
      }
      if (bestScore < confThreshold) continue;
      const cx = data[0 * numAnchors + i];
      const cy = data[1 * numAnchors + i];
      const bw = data[2 * numAnchors + i];
      const bh = data[3 * numAnchors + i];
      const x1 = clamp((cx - bw / 2 - padX) / scale, 0, w - 1);
      const y1 = clamp((cy - bh / 2 - padY) / scale, 0, h - 1);
      const x2 = clamp((cx + bw / 2 - padX) / scale, 0, w - 1);
      const y2 = clamp((cy + bh / 2 - padY) / scale, 0, h - 1);
      candidates.push({ x1, y1, x2, y2, conf: bestScore, cls: bestCls, name: COCO_NAMES[bestCls] });
    }

    candidates.sort((a, b) => b.conf - a.conf);
    const kept: Box[] = [];
    for (const c of candidates) {
      if (kept.some((k) => k.cls === c.cls && iou(k, c) > 0.45)) continue;
      kept.push(c);
      if (kept.length >= 100) break;
    }
    return { boxes: kept, w, h };
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

const camera = new MJPEGCamera();
const engine = new YoloEngine();

let latestMeta: Meta = EMPTY_META;
let latestRawJpeg: Buffer | null = null;
let confidence = 0.25;

let pendingJpeg: Buffer | null = null;
let inferenceBusy = false;
let lastInferenceTs = 0;

camera.onFrame((jpeg) => {
  latestRawJpeg = jpeg;
  pendingJpeg = jpeg;
  // Broadcast to clients immediately at camera rate.
  const metaText = JSON.stringify(latestMeta);
  for (const client of camera.clientList) {
    if (client.readyState !== WebSocket.OPEN) continue;
    try {
      client.send(metaText);
      client.send(jpeg);
    } catch (e) {
      console.error("[ws] send failed", e);
    }
  }
  // Schedule inference (debounced).
  void runInferenceIfReady();
});

async function runInferenceIfReady(): Promise<void> {
  if (inferenceBusy) return;
  const now = performance.now();
  if (now - lastInferenceTs < MIN_INTERVAL_MS) return;
  const jpeg = pendingJpeg;
  if (!jpeg) return;
  pendingJpeg = null;
  inferenceBusy = true;
  try {
    const t0 = performance.now();
    const { boxes, w, h } = await engine.infer(jpeg, confidence);
    const inferenceMs = performance.now() - t0;
    lastInferenceTs = performance.now();
    const classes: Record<string, number> = {};
    for (const b of boxes) classes[b.name] = (classes[b.name] ?? 0) + 1;
    latestMeta = {
      detections: boxes.length,
      inference_ms: Math.round(inferenceMs * 10) / 10,
      classes,
      boxes,
      frame_w: w,
      frame_h: h,
    };
  } catch (e) {
    console.error("[yolo] inference error", e);
  } finally {
    inferenceBusy = false;
  }
}

// ---------------------------------------------------------------------------
// HTTP + WebSocket server
// ---------------------------------------------------------------------------

const app = express();
const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/stream" });

wss.on("connection", (ws) => {
  console.log("[ws] client connected");
  camera.addClient(ws);
  // Prime the new client with the most recent frame + meta if we have one.
  if (latestRawJpeg) {
    try {
      ws.send(JSON.stringify(latestMeta));
      ws.send(latestRawJpeg);
    } catch {}
  }
  ws.on("message", (msg) => {
    try {
      const data = JSON.parse(msg.toString());
      if (typeof data.switch_camera === "string") {
        console.log(`[ws] switching camera to ${data.switch_camera}`);
        camera.switchCamera(data.switch_camera);
      }
      if (typeof data.confidence === "number") {
        confidence = clamp(data.confidence, 0.05, 0.95);
        console.log(`[yolo] confidence -> ${confidence.toFixed(2)}`);
      }
    } catch {}
  });
  ws.on("close", () => {
    console.log("[ws] client disconnected");
    camera.removeClient(ws);
  });
});

app.get("/", (_req, res) => {
  res.sendFile(path.resolve(APP_ROOT, "index.html"));
});

app.use("/assets", express.static(path.resolve(APP_ROOT, "assets")));

app.get("/cameras", (_req, res) => {
  try {
    const output = execSync("v4l2-ctl --list-devices", { encoding: "utf-8", timeout: 5000 });
    const cameras: { id: string; name: string }[] = [];
    let currentName = "";
    for (const line of output.split("\n")) {
      if (!line) continue;
      if (!line.startsWith("\t")) {
        currentName = line.replace(/:$/, "").trim();
      } else {
        const dev = line.trim();
        if (dev.startsWith("/dev/video")) cameras.push({ id: dev, name: currentName });
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

(async () => {
  try {
    await engine.load();
    console.log("[yolo] model loaded");
  } catch (e) {
    console.error("[yolo] failed to load model:", e);
  }
  server.listen(PORT, () => {
    console.log(`Camera feed (YOLO) server listening on http://${WENDY_HOSTNAME}:${PORT}`);
  });
})();

process.on("SIGINT", () => { camera.shutdown(); process.exit(0); });
process.on("SIGTERM", () => { camera.shutdown(); process.exit(0); });
