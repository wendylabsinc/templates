import express from "express";
import { createServer } from "http";
import { WebSocketServer, WebSocket } from "ws";
import { spawn, execSync, type ChildProcess } from "child_process";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";

const PORT = parseInt(process.env.PORT ?? "{{.PORT}}", 10);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// GStreamer pipeline management
// ---------------------------------------------------------------------------

let gstProcess: ChildProcess | null = null;
let currentDevice = "/dev/video0";

function startPipeline(device: string): void {
    stopPipeline();
    currentDevice = device;

    console.log(`[gst] starting pipeline for ${device}`);

    gstProcess = spawn("gst-launch-1.0", [
        "v4l2src",
        `device=${device}`,
        "!",
        "image/jpeg",
        "!",
        "fdsink",
        "fd=1",
    ]);

    let buffer = Buffer.alloc(0);

    gstProcess.stdout?.on("data", (chunk: Buffer) => {
        buffer = Buffer.concat([buffer, chunk]);

        // Scan for complete JPEG frames (FFD8 start, FFD9 end)
        while (true) {
            const start = findMarker(buffer, 0xff, 0xd8);
            if (start === -1) {
                // No start marker — discard everything
                buffer = Buffer.alloc(0);
                break;
            }

            // Drop bytes before the start marker
            if (start > 0) {
                buffer = buffer.subarray(start);
            }

            const end = findMarker(buffer, 0xff, 0xd9, 2);
            if (end === -1) {
                // Incomplete frame — wait for more data
                break;
            }

            const frameEnd = end + 2; // include the FFD9 marker
            const frame = buffer.subarray(0, frameEnd);
            buffer = buffer.subarray(frameEnd);

            broadcastFrame(frame);
        }
    });

    gstProcess.stderr?.on("data", (data: Buffer) => {
        console.error(`[gst] ${data.toString()}`);
    });

    gstProcess.on("close", (code) => {
        console.log(`[gst] process exited with code ${code}`);
        gstProcess = null;
    });
}

function stopPipeline(): void {
    if (gstProcess) {
        gstProcess.kill("SIGTERM");
        gstProcess = null;
    }
}

function findMarker(buf: Buffer, b0: number, b1: number, offset = 0): number {
    for (let i = offset; i < buf.length - 1; i++) {
        if (buf[i] === b0 && buf[i + 1] === b1) {
            return i;
        }
    }
    return -1;
}

// ---------------------------------------------------------------------------
// WebSocket — broadcast JPEG frames
// ---------------------------------------------------------------------------

const app = express();
const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/stream" });

function broadcastFrame(frame: Buffer): void {
    for (const client of wss.clients) {
        if (client.readyState === WebSocket.OPEN) {
            client.send(frame);
        }
    }
}

wss.on("connection", (ws) => {
    console.log("[ws] client connected");

    ws.on("message", (msg) => {
        try {
            const data = JSON.parse(msg.toString());
            if (data.action === "switch" && typeof data.device === "string") {
                console.log(`[ws] switching camera to ${data.device}`);
                startPipeline(data.device);
            }
        } catch {
            // ignore non-JSON messages
        }
    });

    ws.on("close", () => {
        console.log("[ws] client disconnected");
    });

    // Start the pipeline on first connection if not already running
    if (!gstProcess) {
        startPipeline(currentDevice);
    }
});

// ---------------------------------------------------------------------------
// HTTP routes
// ---------------------------------------------------------------------------

app.get("/cameras", (_req, res) => {
    try {
        const output = execSync("v4l2-ctl --list-devices", {
            encoding: "utf-8",
            timeout: 5000,
        });

        const devices: { name: string; paths: string[] }[] = [];
        let current: { name: string; paths: string[] } | null = null;

        for (const line of output.split("\n")) {
            if (line.length === 0) {
                continue;
            }
            if (!line.startsWith("\t")) {
                current = { name: line.replace(/:$/, "").trim(), paths: [] };
                devices.push(current);
            } else if (current) {
                const devPath = line.trim();
                if (devPath.startsWith("/dev/video")) {
                    current.paths.push(devPath);
                }
            }
        }

        res.json({ devices, current: currentDevice });
    } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        res.status(500).json({ error: "Failed to list cameras", details: message });
    }
});

app.get("/", (_req, res) => {
    const htmlPath = path.resolve(__dirname, "..", "index.html");
    if (fs.existsSync(htmlPath)) {
        res.sendFile(htmlPath);
    } else {
        // Fallback: look next to the compiled JS (Docker layout)
        const altPath = path.resolve(__dirname, "index.html");
        res.sendFile(altPath);
    }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

server.listen(PORT, () => {
    console.log(`Camera feed server listening on http://0.0.0.0:${PORT}`);
});

process.on("SIGINT", () => {
    stopPipeline();
    process.exit(0);
});

process.on("SIGTERM", () => {
    stopPipeline();
    process.exit(0);
});
