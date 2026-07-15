#!/usr/bin/env python3
"""
Nemotron Speech ASR Web Demo with VAD

A FastAPI web app that captures audio from a USB webcam, uses Silero VAD
for voice activity detection, and transcribes speech using NVIDIA Nemotron.

Features:
- Silero VAD for intelligent speech detection
- Only transcribes when speech is detected
- Real-time waveform visualization
"""
import asyncio
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from collections import deque

import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from scipy import signal
from scipy.io import wavfile

app = FastAPI()

# Audio settings
SAMPLE_RATE = 16000  # Both VAD and Nemotron expect 16kHz
CHANNELS = 1  # Mono

# VAD settings - Silero VAD requires exactly 512 samples at 16kHz (32ms)
VAD_WINDOW_SAMPLES = 512  # Silero VAD window size (fixed)
VAD_WINDOW_MS = VAD_WINDOW_SAMPLES * 1000 // SAMPLE_RATE  # 32ms
RECORD_CHUNK_MS = 96  # Record in 96ms chunks (~10fps, 3 VAD windows per chunk)
RECORD_CHUNK_SAMPLES = int(SAMPLE_RATE * RECORD_CHUNK_MS / 1000)
MIN_SPEECH_MS = 300  # Minimum speech duration to transcribe
MIN_SPEECH_SAMPLES = int(SAMPLE_RATE * MIN_SPEECH_MS / 1000)
MAX_SPEECH_MS = 30000  # Maximum speech duration (30 seconds)
MAX_SPEECH_SAMPLES = int(SAMPLE_RATE * MAX_SPEECH_MS / 1000)
SILENCE_THRESHOLD_MS = 800  # Wait 0.8s of silence before ending utterance
# Number of chunks of silence to end speech
SILENCE_THRESHOLD_CHUNKS = int(SILENCE_THRESHOLD_MS / RECORD_CHUNK_MS)
# Pre-buffer: keep last N chunks to include audio before speech detection
PRE_BUFFER_MS = 400  # Include 400ms of audio before speech started
PRE_BUFFER_CHUNKS = int(PRE_BUFFER_MS / RECORD_CHUNK_MS)

# Models (loaded lazily)
asr_model = None
vad_model = None
model_loading = False
model_error = None


def load_models():
    """Load both VAD and ASR models."""
    global asr_model, vad_model, model_loading, model_error
    model_loading = True

    try:
        # Load Silero VAD
        print("Loading Silero VAD model...")
        vad_model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=False
        )
        print("Silero VAD loaded successfully")

        # Load Nemotron ASR
        print("Loading Nemotron ASR model...")
        import nemo.collections.asr as nemo_asr
        asr_model = nemo_asr.models.ASRModel.from_pretrained(
            model_name="nvidia/nemotron-speech-streaming-en-0.6b"
        )
        print("Nemotron ASR loaded successfully")

        model_error = None
    except Exception as e:
        import traceback
        model_error = str(e)
        print(f"Model loading error: {e}\n{traceback.format_exc()}")
        asr_model = None
        vad_model = None
    finally:
        model_loading = False


def compute_spectrogram(audio_data: np.ndarray, sample_rate: int) -> list:
    """Compute spectrogram from audio data for visualization."""
    if len(audio_data) == 0:
        return []

    nperseg = min(256, len(audio_data))
    if nperseg < 16:
        return []

    frequencies, times, Sxx = signal.spectrogram(
        audio_data,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        scaling='spectrum'
    )

    Sxx_db = 10 * np.log10(Sxx + 1e-10)
    Sxx_normalized = (Sxx_db - Sxx_db.min()) / (Sxx_db.max() - Sxx_db.min() + 1e-10)

    return Sxx_normalized.T.tolist()


def record_audio_chunk(duration_ms: int, device: str | None = None) -> np.ndarray | None:
    """Record a chunk of audio and return as numpy array."""
    duration_s = duration_ms / 1000.0

    cmd = [
        "arecord",
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE),
        "-c", str(CHANNELS),
        "-d", str(int(duration_s) + 1),  # Round up
        "-q",
        "-t", "raw",
        "-"
    ]
    if device:
        cmd = ["arecord", "-D", device] + cmd[1:]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=duration_s + 2
        )
        if result.returncode == 0:
            audio = np.frombuffer(result.stdout, dtype=np.int16)
            # Trim to exact duration
            expected_samples = int(SAMPLE_RATE * duration_s)
            return audio[:expected_samples]
    except Exception as e:
        print(f"Recording error: {e}")

    return None


class VADTranscriptionManager:
    """Manages VAD-based audio capture and transcription."""

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._audio_device: str | None = "plughw:2,0"

        # VAD state
        self._speech_buffer: list[np.ndarray] = []
        self._pre_buffer: deque = deque(maxlen=PRE_BUFFER_CHUNKS)  # Ring buffer for pre-speech audio
        self._silence_chunks = 0
        self._is_speaking = False
        self._vad_threshold = 0.5  # VAD probability threshold

    def _process_vad_windows(self, audio_chunk: np.ndarray) -> tuple[bool, float]:
        """Process audio through VAD in 512-sample windows.

        Returns (is_speech, avg_probability) where is_speech is True if
        majority of windows exceed the threshold.
        """
        global vad_model

        audio_float = audio_chunk.astype(np.float32) / 32768.0
        probs = []
        speech_windows = 0
        total_windows = 0

        # Process in 512-sample windows
        for i in range(0, len(audio_float) - VAD_WINDOW_SAMPLES + 1, VAD_WINDOW_SAMPLES):
            window = audio_float[i:i + VAD_WINDOW_SAMPLES]
            audio_tensor = torch.from_numpy(window)
            prob = vad_model(audio_tensor, SAMPLE_RATE).item()
            probs.append(prob)
            total_windows += 1
            if prob > self._vad_threshold:
                speech_windows += 1

        avg_prob = sum(probs) / len(probs) if probs else 0.0

        # Consider speech if majority of windows have speech
        # (or at least half for small chunks)
        is_speech = total_windows > 0 and speech_windows >= (total_windows / 2)
        return is_speech, avg_prob

    async def _vad_loop(self):
        """Main loop: continuously monitor audio with VAD."""
        global asr_model, vad_model, model_loading, model_error

        # Start model loading in background if not already loaded
        if (asr_model is None or vad_model is None) and not model_loading:
            threading.Thread(target=load_models, daemon=True).start()

        while self._running and self._clients:
            try:
                # Wait for models to load
                if model_loading:
                    await self._broadcast({
                        "type": "status",
                        "status": "loading",
                        "message": "Loading VAD and ASR models..."
                    })
                    await asyncio.sleep(1)
                    continue

                if model_error:
                    await self._broadcast({
                        "type": "status",
                        "status": "error",
                        "message": f"Model error: {model_error}"
                    })
                    await asyncio.sleep(2)
                    continue

                if vad_model is None or asr_model is None:
                    await asyncio.sleep(0.5)
                    continue

                # Record a chunk for VAD processing
                audio_chunk = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: record_audio_chunk(RECORD_CHUNK_MS, self._audio_device)
                )

                if audio_chunk is None or len(audio_chunk) == 0:
                    await self._broadcast({
                        "type": "status",
                        "status": "error",
                        "message": "Failed to record audio"
                    })
                    await asyncio.sleep(1)
                    continue

                # Send waveform data for visualization (send raw samples for smoother display)
                waveform = audio_chunk.astype(np.float32) / 32768.0
                await self._broadcast({
                    "type": "waveform",
                    "data": waveform.tolist(),
                    "sample_rate": SAMPLE_RATE,
                    "duration": RECORD_CHUNK_MS / 1000
                })

                # Run VAD on the chunk (processes in 512-sample windows)
                is_speech, speech_prob = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._process_vad_windows(audio_chunk)
                )

                # State machine for speech detection
                if is_speech:
                    self._silence_chunks = 0

                    if not self._is_speaking:
                        # Speech started - include pre-buffer audio
                        self._is_speaking = True
                        self._speech_buffer = list(self._pre_buffer)  # Include recent audio
                        pre_buf_len = len(self._pre_buffer)
                        self._pre_buffer.clear()
                        print(f"Speech STARTED (prob: {speech_prob:.2f}, pre-buffer: {pre_buf_len} chunks)")
                        await self._broadcast({
                            "type": "status",
                            "status": "recording",
                            "message": "Speech detected..."
                        })
                        await self._broadcast({
                            "type": "log",
                            "level": "info",
                            "message": f"Speech started (prob: {speech_prob:.2f})"
                        })

                    # Add to buffer
                    self._speech_buffer.append(audio_chunk)

                    # Check max duration
                    total_samples = sum(len(c) for c in self._speech_buffer)
                    if total_samples >= MAX_SPEECH_SAMPLES:
                        await self._transcribe_buffer()

                else:
                    if self._is_speaking:
                        self._silence_chunks += 1
                        # Keep buffering during short silences
                        self._speech_buffer.append(audio_chunk)

                        if self._silence_chunks >= SILENCE_THRESHOLD_CHUNKS:
                            # Speech ended - transcribe
                            print(f"Speech ENDED (silence chunks: {self._silence_chunks}, buffer: {len(self._speech_buffer)} chunks)")
                            await self._transcribe_buffer()
                    else:
                        # Not speaking - add to pre-buffer for next speech start
                        self._pre_buffer.append(audio_chunk)
                        # Just listening, update status periodically
                        await self._broadcast({
                            "type": "status",
                            "status": "listening",
                            "message": "Listening..."
                        })

            except Exception as e:
                import traceback
                print(f"VAD loop error: {e}\n{traceback.format_exc()}")
                await self._broadcast({
                    "type": "status",
                    "status": "error",
                    "message": f"Error: {e}"
                })
                await asyncio.sleep(1)

            # Small delay to prevent CPU spinning
            await asyncio.sleep(0.01)

        self._running = False

    async def _transcribe_buffer(self):
        """Transcribe the accumulated speech buffer."""
        global asr_model

        if not self._speech_buffer:
            self._is_speaking = False
            self._silence_chunks = 0
            return

        # Combine all chunks
        audio_data = np.concatenate(self._speech_buffer)
        duration_ms = len(audio_data) / SAMPLE_RATE * 1000
        num_chunks = len(self._speech_buffer)

        # Reset state
        self._is_speaking = False
        self._silence_chunks = 0
        self._speech_buffer = []

        # Check audio levels
        audio_rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
        audio_max = np.max(np.abs(audio_data))
        print(f"Audio stats: duration={duration_ms:.0f}ms, chunks={num_chunks}, rms={audio_rms:.0f}, max={audio_max}")

        # Check minimum duration
        if len(audio_data) < MIN_SPEECH_SAMPLES:
            print(f"Audio too short ({duration_ms:.0f}ms), skipping")
            await self._broadcast({
                "type": "log",
                "level": "debug",
                "message": f"Audio too short ({duration_ms:.0f}ms), skipping"
            })
            return

        await self._broadcast({
            "type": "status",
            "status": "transcribing",
            "message": "Transcribing..."
        })

        print(f"Transcribing {duration_ms:.0f}ms of audio (rms={audio_rms:.0f}, max={audio_max})...")
        await self._broadcast({
            "type": "log",
            "level": "info",
            "message": f"Transcribing {duration_ms:.0f}ms of audio..."
        })

        # Save to temp file for transcription
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_path = f.name
            wavfile.write(wav_path, SAMPLE_RATE, audio_data)

        try:
            # Transcribe
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: asr_model.transcribe([wav_path])
            )

            print(f"Transcription result: {result}")

            # Parse result (Nemotron returns tuple of lists)
            text = ""
            if result:
                if isinstance(result, tuple) and len(result) > 0:
                    first = result[0]
                    if isinstance(first, list) and len(first) > 0:
                        text = str(first[0]).strip()
                    elif isinstance(first, str):
                        text = first.strip()
                elif isinstance(result, list) and len(result) > 0:
                    item = result[0]
                    if isinstance(item, str):
                        text = item.strip()
                    elif isinstance(item, list) and len(item) > 0:
                        text = str(item[0]).strip()

            await self._broadcast({
                "type": "transcription",
                "text": text if text else "(no speech detected)",
                "has_speech": bool(text),
                "duration_ms": int(duration_ms)
            })

            await self._broadcast({
                "type": "log",
                "level": "info",
                "message": f"Transcribed: \"{text}\"" if text else "No speech in audio"
            })

        except Exception as e:
            import traceback
            error_msg = f"Transcription error: {e}"
            print(f"{error_msg}\n{traceback.format_exc()}")
            await self._broadcast({
                "type": "status",
                "status": "error",
                "message": error_msg
            })
            await self._broadcast({
                "type": "log",
                "level": "error",
                "message": error_msg
            })
        finally:
            # Cleanup temp file
            try:
                Path(wav_path).unlink()
            except:
                pass

    async def _broadcast(self, message: dict):
        """Broadcast a message to all connected clients."""
        data = json.dumps(message)
        disconnected = set()

        for ws in self._clients.copy():
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.add(ws)

        self._clients -= disconnected

    async def add_client(self, websocket: WebSocket, device: str | None = None):
        """Add a new client and start processing if needed."""
        async with self._lock:
            self._clients.add(websocket)
            if device:
                self._audio_device = device

            if not self._running:
                self._running = True
                self._task = asyncio.create_task(self._vad_loop())

    async def remove_client(self, websocket: WebSocket):
        """Remove a client and cleanup if no clients remain."""
        async with self._lock:
            self._clients.discard(websocket)

            if not self._clients:
                self._running = False
                if self._task:
                    await self._task
                    self._task = None


manager = VADTranscriptionManager()


@app.on_event("startup")
async def startup_event():
    """Start loading models immediately on startup."""
    global model_loading
    if asr_model is None and vad_model is None and not model_loading:
        print("Starting model preload on startup...")
        threading.Thread(target=load_models, daemon=True).start()


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    """WebSocket endpoint for audio transcription streaming."""
    await websocket.accept()

    device = websocket.query_params.get("device")
    await manager.add_client(websocket, device)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                try:
                    data = json.loads(msg)
                    if data.get("type") == "set_device":
                        manager._audio_device = data.get("device")
                    elif data.get("type") == "set_vad_threshold":
                        manager._vad_threshold = float(data.get("threshold", 0.5))
                except json.JSONDecodeError:
                    pass
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.remove_client(websocket)


@app.get("/status")
async def get_status():
    """Return service status."""
    return {
        "connected_clients": len(manager._clients),
        "active": manager._running,
        "model_loaded": asr_model is not None and vad_model is not None,
        "model_loading": model_loading,
        "model_error": model_error,
        "vad_enabled": vad_model is not None,
        "settings": {
            "sample_rate": SAMPLE_RATE,
            "vad_window_ms": VAD_WINDOW_MS,
            "record_chunk_ms": RECORD_CHUNK_MS,
            "silence_threshold_ms": SILENCE_THRESHOLD_MS,
            "min_speech_ms": MIN_SPEECH_MS,
            "max_speech_ms": MAX_SPEECH_MS,
            "pre_buffer_ms": PRE_BUFFER_MS,
            "vad_threshold": manager._vad_threshold,
        },
    }


@app.get("/")
async def root():
    """Serve the index.html file."""
    index_path = Path(__file__).parent / "index.html"
    return FileResponse(index_path, media_type="text/html")
