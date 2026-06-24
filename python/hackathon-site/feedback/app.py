"""Hackathon Q&A / bug-report inbox.

Tiny FastAPI service behind the page's nginx (which proxies /api/ here).
Submissions are appended to a JSONL file on a persisted volume so they
survive redeploys/restarts. Reading them back requires an admin token.

Binds 127.0.0.1 only: the jetson runs this on host networking, so nginx
(same host) can proxy to it, but it isn't exposed directly on the LAN.
"""
import json
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORE = DATA_DIR / "feedback.jsonl"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "wendy-admin")
PORT = int(os.environ.get("PORT", "8090"))

app = FastAPI(title="hackathon-feedback")


class Submission(BaseModel):
    type: str = Field("question")
    team: str = Field("")
    name: str = Field("")
    message: str = Field(...)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/feedback")
def submit(s: Submission) -> dict:
    msg = (s.message or "").strip()
    if not msg:
        raise HTTPException(400, "message is required")
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        # Normalize to two buckets so the admin view can filter cleanly.
        "type": "bug" if s.type.lower().startswith("bug") else "question",
        "team": (s.team or "").strip()[:80],
        "name": (s.name or "").strip()[:80],
        "message": msg[:4000],
    }
    with STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/api/feedback")
def list_feedback(token: str = "") -> dict:
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "bad or missing token")
    items = []
    if STORE.exists():
        for line in STORE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    items.reverse()  # newest first
    return {"count": len(items), "items": items}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)
