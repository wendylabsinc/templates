#!/usr/bin/env python3
"""
Wendy Voice AI Assistant
A smart voice assistant with wake word detection, speech-to-text,
LLM processing, tool calls, and text-to-speech.
"""
import sys
import time
import numpy as np
import sounddevice as sd
import os
import socket
import urllib.request
import threading
from typing import Optional

import openwakeword
from openwakeword.model import Model
import whisper

# Check ONNX Runtime GPU availability
try:
    import onnxruntime as ort
    print("=" * 60)
    print("GPU CONFIGURATION CHECK")
    print("=" * 60)
    print(f"ONNX Runtime version: {ort.__version__}")
    providers = ort.get_available_providers()
    print(f"Available ONNX providers: {providers}")
    if 'CUDAExecutionProvider' in providers:
        print("✓ GPU acceleration available via CUDA for ONNX")
    else:
        print("⚠ WARNING: CUDAExecutionProvider not found - ONNX running on CPU only")
except ImportError:
    print("⚠ WARNING: ONNX Runtime not installed")

# Check PyTorch CUDA availability
try:
    import torch
    if torch.cuda.is_available():
        print(f"✓ PyTorch CUDA available")
        print(f"  GPU Device: {torch.cuda.get_device_name(0)}")
        print(f"  CUDA Version: {torch.version.cuda}")
        print(f"  PyTorch Version: {torch.__version__}")
    else:
        print("⚠ WARNING: PyTorch CUDA not available - Whisper will run on CPU")
except ImportError:
    print("⚠ WARNING: PyTorch not installed")
print("=" * 60)
print()

# Try to import piper-tts
try:
    import piper
    PIPER_AVAILABLE = True
except ImportError:
    PIPER_AVAILABLE = False
    import subprocess

# Audio / model parameters
SAMPLE_RATE = 16000           # openWakeWord expects 16 kHz PCM
FRAME_DURATION = 0.08         # 80 ms frames (recommended)
FRAME_LENGTH = int(SAMPLE_RATE * FRAME_DURATION)
WAKE_WORD_THRESHOLD = 0.5     # Threshold for wake word detection
BUFFER_DURATION = 3.0         # Seconds to record after wake word

# Default to the smallest official Whisper release for minimal footprint.
DEFAULT_WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny.en")
DEFAULT_WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")


def find_usb_microphone() -> Optional[int]:
    """
    Auto-detect USB microphone device ID.
    Returns the device ID of the first USB microphone found, or None.
    """
    devices = sd.query_devices()

    # Keywords that indicate a USB microphone
    usb_keywords = ['usb', 'brio', 'webcam', 'logitech', 'blue', 'yeti',
                    'snowball', 'at2020', 'rode', 'samson', 'fifine']

    for i, device in enumerate(devices):
        if device['max_input_channels'] > 0:
            name_lower = device['name'].lower()
            for keyword in usb_keywords:
                if keyword in name_lower:
                    print(f"USB microphone auto-detected: Device {i} ({device['name']})")
                    return i

    return None


def check_dns_connectivity(host: str = "github.com", timeout: int = 5) -> bool:
    """Check if DNS resolution and network connectivity are working."""
    print(f"Checking DNS resolution for {host}...")

    # Step 1: Check DNS resolution
    try:
        ip_address = socket.gethostbyname(host)
        print(f"  DNS resolution successful: {host} -> {ip_address}")
    except socket.gaierror as e:
        print(f"  DNS resolution failed: {e}")
        print(f"  Error: Cannot resolve hostname '{host}'")
        print(f"  Possible causes:")
        print(f"    - DNS server not configured (check /etc/resolv.conf)")
        print(f"    - Network connectivity issues")
        print(f"    - Firewall blocking DNS (port 53)")
        return False
    except Exception as e:
        print(f"  Unexpected error during DNS resolution: {e}")
        return False

    # Step 2: Check HTTP connectivity
    print(f"Checking HTTP connectivity to {host}...")
    try:
        urllib.request.urlopen(f"https://{host}", timeout=timeout)
        print(f"  HTTP connectivity successful")
        return True
    except urllib.error.URLError as e:
        print(f"  HTTP connectivity failed: {e}")
        print(f"  Error: Can resolve {host} but cannot connect")
        print(f"  Possible causes:")
        print(f"    - Firewall blocking HTTPS (port 443)")
        print(f"    - Proxy configuration needed")
        print(f"    - Network routing issues")
        return False
    except Exception as e:
        print(f"  Unexpected error during connectivity test: {e}")
        return False


class WhisperSTT:
    """Speech-to-text using OpenAI Whisper."""

    def __init__(
        self,
        model_name: str = DEFAULT_WHISPER_MODEL,
        language: str = DEFAULT_WHISPER_LANGUAGE,
    ):
        import torch

        requested_device = os.environ.get("WHISPER_DEVICE", "auto").strip().lower()
        if requested_device in ("cuda", "gpu"):
            if torch.cuda.is_available():
                self.device = "cuda"
            else:
                self.device = "cpu"
                print("⚠ WHISPER_DEVICE=cuda requested, but CUDA is not available. Falling back to CPU.")
        elif requested_device == "cpu":
            self.device = "cpu"
        else:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        if self.device == "cuda":
            print("✓ Whisper will use GPU (CUDA)")
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
            print(f"  CUDA Version: {torch.version.cuda}")
        else:
            if requested_device == "cpu":
                print("Whisper will use CPU (WHISPER_DEVICE=cpu)")
            else:
                print("⚠ Whisper will use CPU (CUDA not available)")

        print(f"Loading Whisper model: {model_name}...")
        print(f"  Step 1: Download/cache check complete")
        print(f"  Step 2: Initializing model on {self.device}...")
        print(f"  (This may take several minutes on first run due to JIT compilation)")
        sys.stdout.flush()

        try:
            progress_interval = float(os.environ.get("WHISPER_LOAD_PROGRESS_SECS", "30"))
        except ValueError:
            progress_interval = 30.0
        try:
            warn_after = float(os.environ.get("WHISPER_LOAD_WARN_SECS", "120"))
        except ValueError:
            warn_after = 120.0

        _start = time.time()
        _stop_event = threading.Event()

        def _log_progress():
            warned = False
            while not _stop_event.wait(progress_interval):
                elapsed = time.time() - _start
                print(f"Whisper load in progress on {self.device} ({elapsed:.0f}s)...", flush=True)
                if not warned and elapsed >= warn_after:
                    print("If this is taking too long, try WHISPER_DEVICE=cpu to force CPU load.", flush=True)
                    warned = True

        if progress_interval > 0:
            threading.Thread(target=_log_progress, daemon=True).start()

        try:
            self.model = whisper.load_model(model_name, device=self.device)
        finally:
            _stop_event.set()
        _elapsed = time.time() - _start

        self.language = language
        print(f"✓ Whisper model loaded on {self.device} in {_elapsed:.1f}s")

    def transcribe(self, audio_data: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio to text."""
        # Whisper expects 16kHz audio
        if sample_rate != 16000:
            import scipy.signal
            num_samples = int(len(audio_data) * 16000 / sample_rate)
            audio_data = scipy.signal.resample(audio_data, num_samples)

        result = self.model.transcribe(audio_data, language=self.language)
        return result["text"].strip()


class LLMHandler:
    """Handles LLM processing and tool calls."""
    
    def __init__(self):
        # Simple stub for now - replace with actual LLM
        self.tools = {
            "turn_light_on": {
                "function": lambda: "Light turned on",
                "description": "Turns on the light"
            },
            "turn_light_off": {
                "function": lambda: "Light turned off",
                "description": "Turns off the light"
            }
        }
    
    def process_query(self, query: str) -> str:
        """Process query and return response."""
        query_lower = query.lower()
        
        # Simple keyword matching for now
        if "light" in query_lower and ("on" in query_lower or "turn on" in query_lower):
            result = self.tools["turn_light_on"]["function"]()
            return f"I've turned on the light. {result}"
        elif "light" in query_lower and ("off" in query_lower or "turn off" in query_lower):
            result = self.tools["turn_light_off"]["function"]()
            return f"I've turned off the light. {result}"
        else:
            return f"I heard you say: {query}. I'm a simple assistant - try asking me to turn lights on or off."


class PiperTTS:
    """Text-to-speech using Piper TTS."""
    
    def __init__(self, model_path: Optional[str] = None, voice: str = "en_US-lessac-medium"):
        self.model_path = model_path
        self.voice = voice
        self.tts_engine = None
        
        # Determine model file path
        if model_path:
            if os.path.isdir(model_path):
                model_file = os.path.join(model_path, f"{voice}.onnx")
                model_dir = model_path
            else:
                model_file = model_path
                model_dir = os.path.dirname(model_path) if os.path.dirname(model_path) else "."
        else:
            model_dir = "./voices"
            model_file = os.path.join(model_dir, f"{voice}.onnx")
        
        # Ensure directory exists
        os.makedirs(model_dir, exist_ok=True)
        
        # Download model if it doesn't exist
        if not os.path.exists(model_file):
            print(f"Piper model not found at {model_file}")
            print(f"Downloading Piper TTS model: {voice}...")
            self._download_piper_model(model_dir, voice)
        
        if PIPER_AVAILABLE:
            try:
                if os.path.exists(model_file):
                    # Check if GPU is available for Piper TTS
                    use_cuda = False
                    try:
                        import onnxruntime as ort
                        if 'CUDAExecutionProvider' in ort.get_available_providers():
                            use_cuda = True
                            print("✓ Piper TTS will use GPU acceleration")
                    except:
                        pass

                    self.tts_engine = piper.PiperVoice.load(model_file, use_cuda=use_cuda)
                    if hasattr(self.tts_engine, 'config'):
                        self.sample_rate = self.tts_engine.config.sample_rate
                    else:
                        self.sample_rate = 22050
                else:
                    print(f"Warning: Piper model still not found at {model_file}")
                    self.sample_rate = 22050
            except Exception as e:
                print(f"Warning: Could not initialize piper library: {e}")
                self.tts_engine = None
                self.sample_rate = 22050
        else:
            print("Warning: piper-tts library not available")
            self.sample_rate = 22050
    
    def _download_piper_model(self, model_dir: str, voice: str):
        """Download Piper TTS model using piper-tts download utility."""
        if not PIPER_AVAILABLE:
            print("⚠ Piper not available - skipping model download")
            return

        try:
            import subprocess
            import sys

            print(f"Downloading Piper voice model: {voice}...")
            result = subprocess.run(
                [sys.executable, "-m", "piper.download_voices", "--data-dir", model_dir, voice],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode == 0:
                print(f"Piper model downloaded successfully to {model_dir}")
                return
            else:
                print(f"⚠ Download command failed: {result.stderr}")
                print("  Piper TTS will not be available")
        except Exception as e:
            print(f"⚠ Error downloading Piper model: {e}")
            print("  Piper TTS will not be available")
    
    def synthesize(self, text: str) -> bytes:
        """Synthesize text to speech audio."""
        if self.tts_engine:
            try:
                audio_chunks = self.tts_engine.synthesize(text)
                audio_bytes_list = []
                for chunk in audio_chunks:
                    if hasattr(chunk, 'audio_int16_bytes'):
                        audio_bytes_list.append(chunk.audio_int16_bytes)
                    elif hasattr(chunk, 'audio_int16_array'):
                        audio_bytes_list.append(chunk.audio_int16_array.tobytes())
                
                if audio_bytes_list:
                    return b"".join(audio_bytes_list)
                return b""
            except Exception as e:
                print(f"Error in piper synthesis: {e}")
                return b""
        return b""
    
    def speak(self, text: str, assistant=None):
        """Speak text using audio output."""
        audio_data = self.synthesize(text)
        if not audio_data:
            print(f"[TTS] Could not synthesize: {text}")
            # Fallback: print the text to console
            print(f"[TTS FALLBACK] {text}")
            return

        # Note: is_speaking flag should be set by caller BEFORE calling speak()
        # This ensures the audio callback sees it immediately

        audio_array = np.frombuffer(audio_data, dtype=np.int16)

        # Convert to float32 for resampling if needed
        audio_float = audio_array.astype(np.float32) / 32768.0

        # Resample if Piper's sample rate doesn't match system default
        # Most audio systems expect 44100 or 48000 Hz
        target_rate = 48000  # Common system sample rate
        if self.sample_rate != target_rate:
            try:
                import scipy.signal
                num_samples = int(len(audio_float) * target_rate / self.sample_rate)
                audio_float = scipy.signal.resample(audio_float, num_samples)
            except Exception as e:
                print(f"[TTS] Warning: Could not resample audio: {e}")
                # Try with original sample rate
                target_rate = self.sample_rate

        # Convert back to int16
        audio_resampled = (audio_float * 32768.0).astype(np.int16)

        # Play audio using sounddevice
        try:
            sd.play(audio_resampled, samplerate=target_rate)
            sd.wait()  # Wait until playback is finished
        except Exception as e:
            print(f"[TTS] Error playing audio: {e}")
            print(f"[TTS FALLBACK] {text}")


class WendyAssistant:
    """Main assistant class."""
    
    def __init__(self, 
                 whisper_model: str = DEFAULT_WHISPER_MODEL,
                 whisper_language: str = DEFAULT_WHISPER_LANGUAGE,
                 piper_model_path: Optional[str] = None,
                 piper_voice: str = "en_US-lessac-medium"):
        """Initialize Wendy Assistant."""
        # Initialize components
        self.stt = WhisperSTT(model_name=whisper_model, language=whisper_language)
        self.llm = LLMHandler()
        
        # Set default Piper paths
        if not piper_model_path or piper_model_path == "voices":
            if os.path.exists("/app/voices"):
                piper_model_path = "/app/voices"
            elif os.path.exists("./voices"):
                piper_model_path = "./voices"
        
        self.tts = PiperTTS(model_path=piper_model_path, voice=piper_voice)
        
        # Audio buffer for recording after wake word
        self.audio_buffer = []
        self.is_recording = False
        self.is_processing = False  # Flag to disable wake word detection during processing
        self.is_speaking = False  # Flag to disable wake word detection during TTS playback
        self.samples_to_record = int(BUFFER_DURATION * SAMPLE_RATE)
        self.samples_collected = 0
        
        # Wake word detection state
        self.last_detection_time = 0
        self.cooldown_period = 5.0  # Seconds between detections (increased to prevent echo re-triggers)
    
    def handle_wake_word_detected(self, wake_word: str, score: float):
        """Handle wake word detection - record and process query."""
        current_time = time.time()
        
        # Cooldown to prevent multiple rapid detections
        time_since_last = current_time - self.last_detection_time
        if time_since_last < self.cooldown_period:
            print(f"[SKIP] Too soon since last detection ({time_since_last:.2f}s < {self.cooldown_period}s)")
            # Reset flags since we're not actually processing this detection
            self.is_recording = False
            self.is_processing = False
            return
        
        # Note: Flags are already set in audio_callback, so we don't check them here
        # The check happens in audio_callback before calling this method
        
        self.last_detection_time = current_time
        print(f"\n[WAKE WORD DETECTED] {wake_word} (score={score:.3f})")
        print("Listening...")
        
        # Flags are already set in audio_callback before calling this method
        # Just initialize the audio buffer
        self.audio_buffer = []
        self.samples_collected = 0
    
    def process_recorded_audio(self):
        """Process the recorded audio buffer."""
        if len(self.audio_buffer) == 0:
            self.is_processing = False
            return
        
        # is_processing is already True (set in handle_wake_word_detected)
        # This ensures wake word detection stays disabled during processing
        
        try:
            # Concatenate audio buffer
            audio_data = np.concatenate(self.audio_buffer)
            
            # Convert from int16 to float32 for Whisper
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # Transcribe
            print("Transcribing...")
            transcription = self.stt.transcribe(audio_float, SAMPLE_RATE)
            print(f"You said: {transcription}")
            
            if not transcription or len(transcription.strip()) == 0:
                print("No speech detected.")
                return
            
            # Process with LLM
            print("Processing with LLM...")
            response = self.llm.process_query(transcription)
            print(f"Response: {response}")
            
            # Set speaking flag BEFORE calling speak() to prevent audio callback from detecting wake words
            print("Speaking response...")
            self.is_speaking = True
            try:
                self.tts.speak(response, assistant=self)
            finally:
                # Keep speaking flag set longer to prevent echo re-triggers
                # The audio callback will skip wake word detection while is_speaking is True
                time.sleep(1.5)  # Longer delay to let TTS audio fully settle
                self.is_speaking = False
                self.last_detection_time = time.time()
            
            print("\nListening for wake word again...")
        finally:
            # Always reset processing flag AFTER a delay to prevent immediate re-triggers
            time.sleep(0.5)  # Additional delay before re-enabling wake word detection
            self.is_processing = False


def audio_callback(indata, frames, time_info, status, assistant, model):
    """sounddevice callback – runs on the audio thread."""
    if status:
        print(status, file=sys.stderr)

    # indata: shape (frames, channels), dtype=int16
    # Use the first (mono) channel
    pcm16_mono = indata[:, 0]

    # Debug: Print audio level periodically (every ~100 frames = ~8 seconds at 80ms chunks)
    if not hasattr(assistant, '_debug_frame_count'):
        assistant._debug_frame_count = 0
    assistant._debug_frame_count += 1
    if assistant._debug_frame_count % 100 == 0:
        audio_level = np.abs(pcm16_mono).mean()
        print(f"[DEBUG] Audio active - level: {audio_level:.1f}", flush=True)

    # If recording, processing, or speaking, don't check for wake words
    if assistant.is_recording:
        assistant.audio_buffer.append(pcm16_mono.copy())
        assistant.samples_collected += len(pcm16_mono)
        
        # Check if we've recorded enough
        if assistant.samples_collected >= assistant.samples_to_record:
            assistant.is_recording = False
            # Process in main thread (defer to avoid blocking audio callback)
            assistant.process_recorded_audio()
        return
    elif assistant.is_processing or assistant.is_speaking:
        return
    
    # Check for wake words (only when not recording/processing/speaking)
    scores = model.predict(pcm16_mono)

    # Debug: Show highest score periodically
    if assistant._debug_frame_count % 100 == 0:
        max_score = max((float(s) for s in scores.values() if isinstance(s, (float, np.floating, int))), default=0)
        if max_score > 0.1:  # Only show if there's any significant detection
            max_word = max(scores.items(), key=lambda x: float(x[1]) if isinstance(x[1], (float, np.floating, int)) else 0)[0]
            print(f"[DEBUG] Highest wake word score: {max_word}={max_score:.3f} (threshold={WAKE_WORD_THRESHOLD})", flush=True)

    # Check all wake word models
    for wake_word, score in scores.items():
        if isinstance(score, (float, np.floating, int)):
            score_float = float(score)
            if score_float >= WAKE_WORD_THRESHOLD:
                # Double-check flags before handling (race condition protection)
                if not assistant.is_recording and not assistant.is_processing and not assistant.is_speaking:
                    # Set flags IMMEDIATELY to prevent other callback iterations from processing the same wake word
                    assistant.is_processing = True
                    assistant.is_recording = True
                    # Now call handle_wake_word_detected (which will do additional checks)
                    assistant.handle_wake_word_detected(wake_word, score_float)
                break


def main():
    # Set environment variables to suppress ONNX Runtime errors
    os.environ.setdefault('ORT_LOGGING_LEVEL', '3')  # ERROR level only
    os.environ.setdefault('KMP_DUPLICATE_LIB_OK', 'TRUE')  # Suppress OpenMP warnings
    
    # 1) Ensure pre-trained models are present
    print("Checking/downloading openWakeWord models…")

    # Check DNS and network connectivity before attempting download
    can_download = check_dns_connectivity("github.com")

    if can_download:
        try:
            openwakeword.utils.download_models()
        except Exception as e:
            print(f"Warning: Error downloading models: {e}")
            print("Continuing with existing models if available...")
    else:
        print("=" * 60)
        print("WARNING: Cannot connect to github.com")
        print("Skipping model download - will attempt to use existing models")
        print("=" * 60)
        print()
        print("To fix DNS issues in your container:")
        print("  1. Check DNS configuration: cat /etc/resolv.conf")
        print("  2. Add DNS servers if missing:")
        print("     echo 'nameserver 8.8.8.8' >> /etc/resolv.conf")
        print("     echo 'nameserver 8.8.4.4' >> /etc/resolv.conf")
        print("  3. Or use host network: docker run --network=host ...")
        print("  4. Check container DNS: docker run --dns 8.8.8.8 ...")
        print("=" * 60)
        print()
    
    # 2) Create model instance
    print("Initializing openWakeWord model…")

    # Configure inference engine for GPU if available
    inference_framework = 'onnx'  # Use ONNX by default
    try:
        import onnxruntime as ort
        if 'CUDAExecutionProvider' in ort.get_available_providers():
            print("Configuring openWakeWord to use GPU...")
            inference_framework = 'onnx'  # ONNX with CUDA
        else:
            print("Using CPU inference...")
    except ImportError:
        print("ONNX Runtime not found, falling back to TFLite...")
        inference_framework = 'tflite'

    try:
        model = Model(
            vad_threshold=0.5,  # enable built-in Silero VAD gate to reduce false positives
            inference_framework=inference_framework,
        )
        print(f"✓ openWakeWord initialized with {inference_framework} backend")
    except Exception as e:
        print(f"Error initializing openWakeWord model: {e}")
        print("Trying without VAD...")
        try:
            model = Model(inference_framework=inference_framework)  # Try without VAD
            print(f"✓ openWakeWord initialized (no VAD) with {inference_framework} backend")
        except Exception as e2:
            print(f"Fatal error: Could not initialize wake word model: {e2}")
            sys.exit(1)
    
    print(f"Loaded wake word models: {list(model.models.keys())}")
    
    # 3) Initialize assistant
    print("Initializing Wendy Assistant...")
    assistant = WendyAssistant()
    
    # 4) Create callback with assistant and model
    def callback(indata, frames, time_info, status):
        audio_callback(indata, frames, time_info, status, assistant, model)
    
    # 5) Auto-detect USB microphone
    usb_device = find_usb_microphone()
    if usb_device is None:
        print("No USB microphone detected, using default audio device")

    # 6) Open microphone and start streaming
    print("Opening microphone…")
    print("Listening for wake words...")
    print("(Try saying: 'alexa', 'hey jarvis', 'hey mycroft', etc.)")
    print("=" * 60)

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_LENGTH,
        device=usb_device,
        callback=callback,
    ):
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopping.")


if __name__ == "__main__":
    main()
