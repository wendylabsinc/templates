import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { appendFile, readdir, readFile, stat } from "node:fs/promises";
import http from "node:http";
import https from "node:https";
import path from "node:path";

const PORT = Number(process.env.PORT || "3090");
const PUBLIC_DIR = path.resolve("/app/public");
const WORKSPACE_DIR = "/workspace";
const STATE_DIR = "/state";
const HISTORY_PATH = path.join(STATE_DIR, "hermes-history.ndjson");
const HERMES_TOKEN = (process.env.HERMES_TOKEN || "").trim();
const CLAUDE_COMMAND = (process.env.CLAUDE_COMMAND || "claude --print").trim();

const clients = new Set();
const events = [];
let nextEventId = 1;
let activeJob = null;

const mimeTypes = new Map([
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".json", "application/json; charset=utf-8"],
]);

function emit(type, payload) {
  const event = {
    id: nextEventId++,
    type,
    at: new Date().toISOString(),
    payload,
  };
  events.push(event);
  if (events.length > 500) {
    events.splice(0, events.length - 500);
  }

  const encoded = encodeSse(event);
  for (const client of clients) {
    client.write(encoded);
  }
}

function encodeSse(event) {
  return [
    `id: ${event.id}`,
    `event: ${event.type}`,
    `data: ${JSON.stringify(event)}`,
    "",
    "",
  ].join("\n");
}

function sendJson(res, statusCode, body) {
  const data = Buffer.from(JSON.stringify(body, null, 2));
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": data.length,
    "cache-control": "no-store",
  });
  res.end(data);
}

function sendText(res, statusCode, body) {
  res.writeHead(statusCode, {
    "content-type": "text/plain; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(body);
}

function tokenFrom(req, url) {
  const auth = req.headers.authorization || "";
  if (auth.startsWith("Bearer ")) {
    return auth.slice("Bearer ".length).trim();
  }
  const header = req.headers["x-hermes-token"];
  if (typeof header === "string") {
    return header.trim();
  }
  return (url.searchParams.get("token") || "").trim();
}

function isAuthorized(req, url) {
  if (!HERMES_TOKEN) {
    return true;
  }
  return tokenFrom(req, url) === HERMES_TOKEN;
}

function requireAuth(req, url, res) {
  if (isAuthorized(req, url)) {
    return true;
  }
  sendJson(res, 401, {
    error: "unauthorized",
    message: "A valid Hermes access token is required.",
  });
  return false;
}

async function readJsonBody(req) {
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > 1024 * 1024) {
      throw new Error("request body is too large");
    }
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    return {};
  }
  return JSON.parse(raw);
}

async function appendHistory(entry) {
  try {
    await appendFile(HISTORY_PATH, `${JSON.stringify(entry)}\n`);
  } catch (error) {
    emit("warning", { message: `failed to write history: ${error.message}` });
  }
}

function buildClaudePrompt(userPrompt) {
  return [
    "You are Hermes Agent, running on the WendyOS device itself.",
    "The user is giving you a voice or text instruction from the Hermes web console.",
    "Use the local workspace at /workspace for app projects. Use the installed wendy CLI for device inspection, app logs, builds, and deploys.",
    "When asked to build or iterate on an app, create or edit a project under /workspace/apps and run wendy run --yes from that app directory.",
    "Avoid destructive device actions unless the user explicitly asks for them.",
    "",
    "User request:",
    userPrompt.trim(),
  ].join("\n");
}

function runClaude(userPrompt) {
  if (activeJob) {
    return { error: "busy", status: 409 };
  }

  const prompt = buildClaudePrompt(userPrompt);
  const job = {
    id: randomUUID(),
    prompt: userPrompt,
    startedAt: new Date().toISOString(),
    output: "",
    child: null,
  };
  activeJob = job;

  emit("job-start", {
    id: job.id,
    prompt: userPrompt,
    command: CLAUDE_COMMAND,
    cwd: WORKSPACE_DIR,
  });
  void appendHistory({
    type: "job-start",
    id: job.id,
    at: job.startedAt,
    prompt: userPrompt,
  });

  const child = spawn(
    "/bin/sh",
    ["-lc", `${CLAUDE_COMMAND} "$@"`, "hermes-claude", prompt],
    {
      cwd: WORKSPACE_DIR,
      detached: true,
      env: {
        ...process.env,
        NO_COLOR: "1",
        TERM: "dumb",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  job.child = child;

  const onChunk = (stream, chunk) => {
    const text = chunk.toString("utf8");
    job.output += text;
    emit("job-output", { id: job.id, stream, text });
  };

  child.stdout.on("data", (chunk) => onChunk("stdout", chunk));
  child.stderr.on("data", (chunk) => onChunk("stderr", chunk));
  child.on("error", (error) => {
    emit("job-error", { id: job.id, message: error.message });
  });
  child.on("exit", (code, signal) => {
    const finishedAt = new Date().toISOString();
    emit("job-exit", {
      id: job.id,
      code,
      signal,
      finishedAt,
      outputTail: job.output.slice(-2000),
    });
    void appendHistory({
      type: "job-exit",
      id: job.id,
      at: finishedAt,
      code,
      signal,
      outputTail: job.output.slice(-4000),
    });
    activeJob = null;
  });

  return { id: job.id, status: 202 };
}

function cancelActiveJob() {
  if (!activeJob?.child) {
    return false;
  }
  const pid = activeJob.child.pid;
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    try {
      activeJob.child.kill("SIGTERM");
    } catch {
      return false;
    }
  }
  emit("job-cancel", { id: activeJob.id });
  return true;
}

async function workspaceSummary() {
  const entries = await readdir(WORKSPACE_DIR, { withFileTypes: true }).catch(() => []);
  return entries.slice(0, 24).map((entry) => ({
    name: entry.name,
    type: entry.isDirectory() ? "directory" : "file",
  }));
}

async function statusPayload() {
  return {
    activeJob: activeJob
      ? {
          id: activeJob.id,
          prompt: activeJob.prompt,
          startedAt: activeJob.startedAt,
        }
      : null,
    adminSocket: Boolean(process.env.WENDY_AGENT_SOCKET),
    adminSocketPath: process.env.WENDY_AGENT_SOCKET || "",
    buildkitSocket: existsSync("/run/buildkit/buildkitd.sock"),
    claudeHome: existsSync("/root/.claude") || existsSync("/root/.claude.json"),
    workspace: await workspaceSummary(),
  };
}

async function serveStatic(req, res, url) {
  let pathname = url.pathname === "/" ? "/index.html" : url.pathname;
  try {
    pathname = decodeURIComponent(pathname);
  } catch {
    sendText(res, 400, "bad path");
    return;
  }

  const resolved = path.resolve(path.join(PUBLIC_DIR, pathname));
  if (!resolved.startsWith(`${PUBLIC_DIR}${path.sep}`) && resolved !== PUBLIC_DIR) {
    sendText(res, 403, "forbidden");
    return;
  }

  try {
    const info = await stat(resolved);
    if (!info.isFile()) {
      sendText(res, 404, "not found");
      return;
    }
    const ext = path.extname(resolved);
    const body = await readFile(resolved);
    res.writeHead(200, {
      "content-type": mimeTypes.get(ext) || "application/octet-stream",
      "content-length": body.length,
      "cache-control": "no-store",
    });
    res.end(body);
  } catch {
    sendText(res, 404, "not found");
  }
}

async function handleApi(req, res, url) {
  if (url.pathname === "/api/config" && req.method === "GET") {
    sendJson(res, 200, {
      authRequired: Boolean(HERMES_TOKEN),
      appName: "Hermes Agent",
    });
    return;
  }

  if (!requireAuth(req, url, res)) {
    return;
  }

  if (url.pathname === "/api/status" && req.method === "GET") {
    sendJson(res, 200, await statusPayload());
    return;
  }

  if (url.pathname === "/api/events" && req.method === "GET") {
    res.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    });
    const lastId = Number(req.headers["last-event-id"] || url.searchParams.get("lastEventId") || "0");
    for (const event of events) {
      if (event.id > lastId) {
        res.write(encodeSse(event));
      }
    }
    clients.add(res);
    const ping = setInterval(() => {
      res.write(": ping\n\n");
    }, 25000);
    req.on("close", () => {
      clearInterval(ping);
      clients.delete(res);
    });
    return;
  }

  if (url.pathname === "/api/prompt" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const prompt = String(body.prompt || "").trim();
      if (!prompt) {
        sendJson(res, 400, { error: "prompt is required" });
        return;
      }
      const result = runClaude(prompt);
      if (result.error) {
        sendJson(res, result.status, { error: result.error });
        return;
      }
      sendJson(res, 202, { id: result.id });
    } catch (error) {
      sendJson(res, 400, { error: error.message });
    }
    return;
  }

  if (url.pathname === "/api/cancel" && req.method === "POST") {
    sendJson(res, 200, { cancelled: cancelActiveJob() });
    return;
  }

  sendJson(res, 404, { error: "not found" });
}

async function handleRequest(req, res) {
  const host = req.headers.host || `localhost:${PORT}`;
  const url = new URL(req.url || "/", `https://${host}`);
  try {
    if (url.pathname.startsWith("/api/")) {
      await handleApi(req, res, url);
      return;
    }
    await serveStatic(req, res, url);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

function createServer() {
  const certPath = process.env.HTTPS_CERT_PATH || "/state/tls/cert.pem";
  const keyPath = process.env.HTTPS_KEY_PATH || "/state/tls/key.pem";
  if (existsSync(certPath) && existsSync(keyPath)) {
    return https.createServer(
      {
        cert: readFileSync(certPath),
        key: readFileSync(keyPath),
      },
      handleRequest,
    );
  }
  return http.createServer(handleRequest);
}

createServer().listen(PORT, "0.0.0.0", () => {
  emit("server-start", {
    port: PORT,
    https: existsSync(process.env.HTTPS_CERT_PATH || "/state/tls/cert.pem"),
    authRequired: Boolean(HERMES_TOKEN),
  });
  console.log(`Hermes Agent console listening on ${PORT}`);
});
