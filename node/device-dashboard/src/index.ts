import express, { Request, Response } from "express";
import { createServer } from "http";
import path from "path";
import fs from "fs";
import os from "os";
import { fileURLToPath } from "url";
import { execSync, spawn, ChildProcess } from "child_process";
import Database from "better-sqlite3";
import { WebSocketServer, WebSocket } from "ws";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PORT = {{.PORT}};
const WENDY_HOSTNAME = process.env.WENDY_HOSTNAME ?? "localhost";
const DB_PATH = "/data/cars.db";

// ---------------------------------------------------------------------------
// Database (better-sqlite3)
// ---------------------------------------------------------------------------

fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.exec(`
    CREATE TABLE IF NOT EXISTS cars (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        make       TEXT    NOT NULL,
        model      TEXT    NOT NULL,
        color      TEXT    NOT NULL,
        year       INTEGER NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    )
`);

const listCars = db.prepare("SELECT * FROM cars ORDER BY id");
const getCar = db.prepare("SELECT * FROM cars WHERE id = ?");
const insertCar = db.prepare(
    "INSERT INTO cars (make, model, color, year) VALUES (@make, @model, @color, @year)"
);
const updateCar = db.prepare(
    "UPDATE cars SET make = @make, model = @model, color = @color, year = @year, updated_at = datetime('now') WHERE id = @id"
);
const deleteCar = db.prepare("DELETE FROM cars WHERE id = ?");

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

// --- Cars CRUD ---

app.get("/api/cars", (_req: Request, res: Response) => {
    res.json(listCars.all());
});

app.post("/api/cars", (req: Request, res: Response) => {
    const { make, model, color, year } = req.body;
    const info = insertCar.run({ make, model, color, year });
    const car = getCar.get(info.lastInsertRowid);
    res.status(201).json(car);
});

app.get("/api/cars/:id", (req: Request, res: Response) => {
    const car = getCar.get(Number(req.params.id));
    if (!car) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    res.json(car);
});

app.put("/api/cars/:id", (req: Request, res: Response) => {
    const { make, model, color, year } = req.body;
    const id = Number(req.params.id);
    const info = updateCar.run({ make, model, color, year, id });
    if (info.changes === 0) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    res.json(getCar.get(id));
});

app.delete("/api/cars/:id", (req: Request, res: Response) => {
    const info = deleteCar.run(Number(req.params.id));
    if (info.changes === 0) {
        res.status(404).json({ error: "Car not found" });
        return;
    }
    res.status(204).send();
});

// ---------------------------------------------------------------------------
// Device listing helpers
// ---------------------------------------------------------------------------

function listCameras(): { id: string; name: string }[] {
    const cameras: { id: string; name: string }[] = [];
    try {
        const devices = fs
            .readdirSync("/dev")
            .filter((d) => d.startsWith("video"))
            .sort()
            .map((d) => `/dev/${d}`);

        for (const devPath of devices) {
            try {
                const info = execSync(
                    `v4l2-ctl --device ${devPath} --all 2>/dev/null`,
                    { timeout: 2000 }
                ).toString();
                if (!info.includes("Video Capture")) continue;

                let name = path.basename(devPath);
                const ctrlInfo = execSync(
                    `v4l2-ctl --device ${devPath} --info 2>/dev/null`,
                    { timeout: 2000 }
                ).toString();
                for (const line of ctrlInfo.split("\n")) {
                    if (line.includes("Card type")) {
                        name = line.split(":").slice(1).join(":").trim();
                        break;
                    }
                }
                cameras.push({ id: devPath, name });
            } catch {
                // device not queryable, skip
            }
        }
    } catch {
        // /dev not readable or v4l2-ctl missing
    }
    return cameras;
}

function listAlsaDevices(cmd: string): { id: string; name: string }[] {
    const devs: { id: string; name: string }[] = [];
    try {
        const out = execSync(cmd, {
            timeout: 2000,
            stdio: ["pipe", "pipe", "pipe"],
        }).toString();
        for (const line of out.split("\n")) {
            if (line.startsWith("card ")) {
                const parts = line.split(":");
                if (parts.length >= 2) {
                    const card = line.split(/\s+/)[1].replace(/:$/, "");
                    const name = parts[1].trim().split("[")[0].trim();
                    devs.push({ id: `hw:${card},0`, name });
                }
            }
        }
    } catch {
        // arecord/aplay not available
    }
    return devs;
}

// --- Device endpoints ---

app.get("/api/cameras", (_req: Request, res: Response) => {
    res.json(listCameras());
});

app.get("/api/microphones", (_req: Request, res: Response) => {
    res.json(listAlsaDevices("arecord -l"));
});

app.get("/api/speakers", (_req: Request, res: Response) => {
    res.json(listAlsaDevices("aplay -l"));
});

// ---------------------------------------------------------------------------
// GPU info
// ---------------------------------------------------------------------------

app.get("/api/gpu", (_req: Request, res: Response) => {
    let info: Record<string, unknown> = { available: false };
    try {
        const out = execSync(
            "nvidia-smi --query-gpu=name,memory.total,driver_version,temperature.gpu --format=csv,noheader,nounits",
            { timeout: 5000, stdio: ["pipe", "pipe", "pipe"] }
        ).toString().trim();
        if (out) {
            const parts = out.split(",").map((p) => p.trim());
            info = {
                available: true,
                name: parts[0] ?? null,
                memory: parts[1] ? `${parts[1]} MiB` : null,
                driver: parts[2] ?? null,
                temperature: parts[3] ? `${parts[3]}\u00b0C` : null,
            };
        }
    } catch {
        try {
            const temp = fs
                .readFileSync("/sys/class/thermal/thermal_zone0/temp", "utf-8")
                .trim();
            info = {
                available: true,
                name: "ARM GPU",
                temperature: `${(parseInt(temp, 10) / 1000).toFixed(1)}\u00b0C`,
            };
        } catch {
            // no GPU info available
        }
    }
    res.json(info);
});

// ---------------------------------------------------------------------------
// System info
// ---------------------------------------------------------------------------

app.get("/api/system", (_req: Request, res: Response) => {
    const hostname = process.env.WENDY_HOSTNAME ?? os.hostname();

    // Memory from /proc/meminfo
    const mem: Record<string, string> = {};
    try {
        const mi = fs.readFileSync("/proc/meminfo", "utf-8");
        for (const line of mi.split("\n")) {
            if (line.startsWith("MemTotal")) {
                mem.total = `${Math.floor(parseInt(line.split(/\s+/)[1], 10) / 1024)} MB`;
            } else if (line.startsWith("MemAvailable")) {
                mem.free = `${Math.floor(parseInt(line.split(/\s+/)[1], 10) / 1024)} MB`;
            }
        }
        if (mem.total && mem.free) {
            const total = parseInt(mem.total, 10);
            const free = parseInt(mem.free, 10);
            mem.used = `${total - free} MB`;
        }
    } catch {
        // not on Linux
    }

    // Disk
    const disk: Record<string, string> = {};
    try {
        const stat = fs.statfsSync("/");
        const totalBytes = stat.bsize * stat.blocks;
        const freeBytes = stat.bsize * stat.bfree;
        const usedBytes = totalBytes - freeBytes;
        disk.total = `${Math.floor(totalBytes / 1024 ** 3)} GB`;
        disk.used = `${Math.floor(usedBytes / 1024 ** 3)} GB`;
        disk.free = `${Math.floor(freeBytes / 1024 ** 3)} GB`;
    } catch {
        // statfs not available
    }

    // CPU
    const cpu: Record<string, unknown> = {
        model: os.cpus()[0]?.model ?? os.arch(),
        cores: os.cpus().length,
    };
    try {
        const ci = fs.readFileSync("/proc/cpuinfo", "utf-8");
        const models = ci
            .split("\n")
            .filter((l) => l.startsWith("model name"))
            .map((l) => l.split(":")[1]?.trim());
        if (models[0]) cpu.model = models[0];
    } catch {
        // use os.cpus() fallback already set
    }

    // Uptime
    let uptime = "";
    try {
        const secs = parseFloat(
            fs.readFileSync("/proc/uptime", "utf-8").split(/\s+/)[0]
        );
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        uptime = `${h}h ${m}m`;
    } catch {
        // not on Linux
    }

    res.json({
        hostname,
        platform: os.platform(),
        architecture: os.arch(),
        uptime,
        memory: mem,
        disk,
        cpu,
    });
});

// ---------------------------------------------------------------------------
// GStreamer singleton helpers for camera & audio WebSocket streaming
// ---------------------------------------------------------------------------

interface GstSingleton {
    process: ChildProcess | null;
    clients: Set<WebSocket>;
    currentDevice: string | null;
}

// --- Camera (MJPEG) ---

const camera: GstSingleton = { process: null, clients: new Set(), currentDevice: null };

function startCameraPipeline(device?: string): ChildProcess | null {
    const src = device ? `v4l2src device=${device}` : "v4l2src";
    const pipeline = `${src} ! image/jpeg,framerate=30/1 ! fdsink fd=1`;
    try {
        const proc = spawn("gst-launch-1.0", ["-q", ...pipeline.split(/\s+/)], {
            stdio: ["pipe", "pipe", "pipe"],
        });
        return proc;
    } catch {
        return null;
    }
}

function ensureCameraRunning(): void {
    if (camera.process) return;
    const proc = startCameraPipeline(camera.currentDevice ?? undefined);
    if (!proc) return;
    camera.process = proc;

    // Parse JPEG frames from stdout by scanning for FFD8 (SOI) / FFD9 (EOI)
    let buf = Buffer.alloc(0);
    proc.stdout?.on("data", (chunk: Buffer) => {
        buf = Buffer.concat([buf, chunk]);
        // eslint-disable-next-line no-constant-condition
        while (true) {
            const soi = buf.indexOf(Buffer.from([0xff, 0xd8]));
            if (soi === -1) {
                buf = Buffer.alloc(0);
                break;
            }
            const eoi = buf.indexOf(Buffer.from([0xff, 0xd9]), soi + 2);
            if (eoi === -1) break; // incomplete frame
            const frame = buf.subarray(soi, eoi + 2);
            buf = buf.subarray(eoi + 2);
            for (const ws of camera.clients) {
                if (ws.readyState === WebSocket.OPEN) {
                    ws.send(frame);
                }
            }
        }
    });

    proc.on("close", () => {
        if (camera.process === proc) camera.process = null;
    });
    proc.on("error", () => {
        if (camera.process === proc) camera.process = null;
    });
}

function stopCamera(): void {
    if (camera.process) {
        camera.process.kill("SIGTERM");
        camera.process = null;
    }
}

function switchCamera(device: string): void {
    stopCamera();
    camera.currentDevice = device;
    if (camera.clients.size > 0) {
        ensureCameraRunning();
    }
}

// --- Audio (PCM S16LE 16kHz mono) ---

const audio: GstSingleton = { process: null, clients: new Set(), currentDevice: null };

function startAudioPipeline(device?: string): ChildProcess | null {
    const src = device ? `alsasrc device="${device}"` : "alsasrc";
    const pipeline = `${src} ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,channels=1,rate=16000 ! fdsink fd=1`;
    try {
        const proc = spawn("gst-launch-1.0", ["-q", ...pipeline.split(/\s+/)], {
            stdio: ["pipe", "pipe", "pipe"],
        });
        return proc;
    } catch {
        return null;
    }
}

function ensureAudioRunning(): void {
    if (audio.process) return;
    const proc = startAudioPipeline(audio.currentDevice ?? undefined);
    if (!proc) return;
    audio.process = proc;

    // Raw PCM: forward chunks as-is
    proc.stdout?.on("data", (chunk: Buffer) => {
        for (const ws of audio.clients) {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(chunk);
            }
        }
    });

    proc.on("close", () => {
        if (audio.process === proc) audio.process = null;
    });
    proc.on("error", () => {
        if (audio.process === proc) audio.process = null;
    });
}

function stopAudio(): void {
    if (audio.process) {
        audio.process.kill("SIGTERM");
        audio.process = null;
    }
}

function switchMicrophone(device: string): void {
    stopAudio();
    audio.currentDevice = device;
    if (audio.clients.size > 0) {
        ensureAudioRunning();
    }
}

// ---------------------------------------------------------------------------
// HTTP server + WebSocket upgrade
// ---------------------------------------------------------------------------

const server = createServer(app);

const cameraWss = new WebSocketServer({ noServer: true });
const audioWss = new WebSocketServer({ noServer: true });

server.on("upgrade", (req, socket, head) => {
    const { pathname } = new URL(req.url ?? "/", `http://${req.headers.host}`);
    if (pathname === "/api/camera/stream") {
        cameraWss.handleUpgrade(req, socket, head, (ws) =>
            cameraWss.emit("connection", ws, req)
        );
    } else if (pathname === "/api/audio/stream") {
        audioWss.handleUpgrade(req, socket, head, (ws) =>
            audioWss.emit("connection", ws, req)
        );
    } else {
        socket.destroy();
    }
});

cameraWss.on("connection", (ws: WebSocket) => {
    camera.clients.add(ws);
    ensureCameraRunning();

    ws.on("message", (data) => {
        try {
            const msg = JSON.parse(data.toString());
            if (msg.switch_camera) {
                switchCamera(msg.switch_camera);
            }
        } catch {
            // ignore malformed messages
        }
    });

    ws.on("close", () => {
        camera.clients.delete(ws);
        if (camera.clients.size === 0) stopCamera();
    });
});

audioWss.on("connection", (ws: WebSocket) => {
    audio.clients.add(ws);
    ensureAudioRunning();

    ws.on("message", (data) => {
        try {
            const msg = JSON.parse(data.toString());
            if (msg.switch_microphone) {
                switchMicrophone(msg.switch_microphone);
            }
        } catch {
            // ignore malformed messages
        }
    });

    ws.on("close", () => {
        audio.clients.delete(ws);
        if (audio.clients.size === 0) stopAudio();
    });
});

// ---------------------------------------------------------------------------
// Static files & SPA fallback
// ---------------------------------------------------------------------------

const staticDir = path.join(__dirname, "..", "static");
app.use(express.static(staticDir));

app.get("*", (_req: Request, res: Response) => {
    res.sendFile(path.join(staticDir, "index.html"));
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

server.listen(PORT, () => {
    console.log(`Server running on http://${WENDY_HOSTNAME}:${PORT}`);
});
