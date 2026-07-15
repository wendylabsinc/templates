#!/usr/bin/env python3
"""
Speech-to-Text Application for NVIDIA Jetson
Captures audio from USB microphone and transcribes using OpenAI Whisper.
"""
import sys
import time
import argparse
import numpy as np
import sounddevice as sd
import os
from datetime import datetime
from typing import Optional

import whisper

# Check PyTorch CUDA availability
try:
    import torch
    print("=" * 60)
    print("GPU CONFIGURATION CHECK")
    print("=" * 60)
    if torch.cuda.is_available():
        print(f"PyTorch CUDA available")
        print(f"  GPU Device: {torch.cuda.get_device_name(0)}")
        print(f"  CUDA Version: {torch.version.cuda}")
        print(f"  PyTorch Version: {torch.__version__}")
    else:
        print("WARNING: PyTorch CUDA not available - Whisper will run on CPU")
    print("=" * 60)
    print()
except ImportError:
    print("WARNING: PyTorch not installed")

# Audio parameters
SAMPLE_RATE = 16000  # Whisper expects 16 kHz
DEFAULT_CHUNK_DURATION = 5.0  # Default recording chunk in seconds

# Default Whisper configuration
DEFAULT_WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
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
            # Check if device name contains USB mic keywords
            for keyword in usb_keywords:
                if keyword in name_lower:
                    return i

    return None


def list_audio_devices():
    """List all available audio input devices."""
    print("\nAvailable Audio Input Devices:")
    print("-" * 50)
    devices = sd.query_devices()
    input_devices = []
    usb_device = find_usb_microphone()

    for i, device in enumerate(devices):
        if device['max_input_channels'] > 0:
            input_devices.append((i, device))
            markers = []
            if i == sd.default.device[0]:
                markers.append("DEFAULT")
            if i == usb_device:
                markers.append("USB MIC")
            marker_str = f" ({', '.join(markers)})" if markers else ""
            print(f"  [{i}] {device['name']}{marker_str}")
            print(f"      Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']}")
    print("-" * 50)

    if usb_device is not None:
        print(f"\nUSB microphone auto-detected: Device {usb_device}")

    return input_devices


class WhisperSTT:
    """Speech-to-text using OpenAI Whisper."""

    def __init__(
        self,
        model_name: str = DEFAULT_WHISPER_MODEL,
        language: str = DEFAULT_WHISPER_LANGUAGE,
    ):
        import torch

        # Detect device (GPU if available)
        if torch.cuda.is_available():
            self.device = "cuda"
            print(f"Whisper will use GPU (CUDA)")
            print(f"  GPU: {torch.cuda.get_device_name(0)}")
        else:
            self.device = "cpu"
            print(f"Whisper will use CPU (CUDA not available)")

        print(f"Loading Whisper model: {model_name}...")
        start_time = time.time()
        self.model = whisper.load_model(model_name, device=self.device)
        load_time = time.time() - start_time
        self.language = language
        print(f"Whisper model loaded on {self.device} in {load_time:.2f}s")

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> dict:
        """
        Transcribe audio to text.

        Returns dict with 'text', 'language', and 'segments' keys.
        """
        # Whisper expects 16kHz audio
        if sample_rate != 16000:
            import scipy.signal
            num_samples = int(len(audio_data) * 16000 / sample_rate)
            audio_data = scipy.signal.resample(audio_data, num_samples)

        # Ensure audio is float32
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
            if audio_data.max() > 1.0:
                audio_data = audio_data / 32768.0

        result = self.model.transcribe(
            audio_data,
            language=self.language,
            fp16=(self.device == "cuda")
        )
        return {
            "text": result["text"].strip(),
            "language": result.get("language", self.language),
            "segments": result.get("segments", [])
        }


class ContinuousTranscriber:
    """Continuously transcribe audio from microphone."""

    def __init__(
        self,
        stt: WhisperSTT,
        chunk_duration: float = DEFAULT_CHUNK_DURATION,
        device_id: Optional[int] = None,
        output_file: Optional[str] = None,
        silence_threshold: float = 0.01,
        min_audio_length: float = 0.5,
    ):
        self.stt = stt
        self.chunk_duration = chunk_duration
        self.device_id = device_id
        self.output_file = output_file
        self.silence_threshold = silence_threshold
        self.min_audio_length = min_audio_length

        # Audio buffer
        self.audio_buffer = []
        self.is_recording = True

        # Statistics
        self.total_transcriptions = 0
        self.total_audio_seconds = 0

    def _is_silent(self, audio_data: np.ndarray) -> bool:
        """Check if audio is mostly silence."""
        rms = np.sqrt(np.mean(audio_data ** 2))
        return rms < self.silence_threshold

    def _save_transcription(self, text: str, timestamp: str):
        """Save transcription to file if output file is specified."""
        if self.output_file and text.strip():
            with open(self.output_file, 'a') as f:
                f.write(f"[{timestamp}] {text}\n")

    def audio_callback(self, indata, frames, time_info, status):
        """Audio callback for sounddevice."""
        if status:
            print(f"Audio status: {status}", file=sys.stderr)

        # Store audio data
        self.audio_buffer.append(indata[:, 0].copy())

    def process_buffer(self) -> Optional[str]:
        """Process accumulated audio buffer and return transcription."""
        if not self.audio_buffer:
            return None

        # Concatenate buffer
        audio_data = np.concatenate(self.audio_buffer)
        self.audio_buffer = []

        # Check minimum length
        audio_length = len(audio_data) / SAMPLE_RATE
        if audio_length < self.min_audio_length:
            return None

        # Convert to float32
        audio_float = audio_data.astype(np.float32) / 32768.0

        # Skip if silent
        if self._is_silent(audio_float):
            return None

        # Transcribe
        try:
            result = self.stt.transcribe(audio_float, SAMPLE_RATE)
            text = result["text"]

            if text.strip():
                self.total_transcriptions += 1
                self.total_audio_seconds += audio_length
                return text
        except Exception as e:
            print(f"Transcription error: {e}", file=sys.stderr)

        return None

    def run(self):
        """Run continuous transcription loop."""
        print("\n" + "=" * 60)
        print("SPEECH-TO-TEXT - Continuous Mode")
        print("=" * 60)
        print(f"Chunk duration: {self.chunk_duration}s")
        print(f"Device: {self.device_id if self.device_id is not None else 'default'}")
        if self.output_file:
            print(f"Output file: {self.output_file}")
        print("Press Ctrl+C to stop")
        print("=" * 60 + "\n")

        samples_per_chunk = int(self.chunk_duration * SAMPLE_RATE)

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=4096,  # Larger buffer for Jetson Orin
                device=self.device_id,
                callback=self.audio_callback,
            ):
                print("Listening...\n")

                while self.is_recording:
                    # Wait for chunk duration
                    time.sleep(self.chunk_duration)

                    # Process buffer
                    text = self.process_buffer()

                    if text:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{timestamp}] {text}")
                        self._save_transcription(text, timestamp)

        except KeyboardInterrupt:
            print("\n\nStopping...")

        # Print statistics
        print("\n" + "=" * 60)
        print("SESSION STATISTICS")
        print("=" * 60)
        print(f"Total transcriptions: {self.total_transcriptions}")
        print(f"Total audio processed: {self.total_audio_seconds:.1f}s")
        print("=" * 60)


def transcribe_single(stt: WhisperSTT, duration: float, device_id: Optional[int] = None):
    """Record and transcribe a single audio segment."""
    print(f"\nRecording for {duration} seconds...")

    # Record audio
    audio_data = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=device_id,
    )
    sd.wait()

    print("Processing...")

    # Convert to float32
    audio_float = audio_data[:, 0].astype(np.float32) / 32768.0

    # Transcribe
    start_time = time.time()
    result = stt.transcribe(audio_float, SAMPLE_RATE)
    transcribe_time = time.time() - start_time

    print("\n" + "=" * 60)
    print("TRANSCRIPTION RESULT")
    print("=" * 60)
    print(f"Text: {result['text']}")
    print(f"Language: {result['language']}")
    print(f"Processing time: {transcribe_time:.2f}s")
    print("=" * 60)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Speech-to-Text for NVIDIA Jetson with USB Microphone"
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=DEFAULT_WHISPER_MODEL,
        choices=["tiny", "tiny.en", "base", "base.en", "small", "small.en",
                 "medium", "medium.en", "large", "large-v2", "large-v3"],
        help="Whisper model to use (default: base)"
    )
    parser.add_argument(
        "--language", "-l",
        type=str,
        default=DEFAULT_WHISPER_LANGUAGE,
        help="Language code for transcription (default: en)"
    )
    parser.add_argument(
        "--device", "-d",
        type=int,
        default=None,
        help="Audio input device ID (use --list-devices to see available)"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices and exit"
    )
    parser.add_argument(
        "--duration", "-t",
        type=float,
        default=DEFAULT_CHUNK_DURATION,
        help="Recording duration in seconds (default: 5.0)"
    )
    parser.add_argument(
        "--continuous", "-c",
        action="store_true",
        help="Run in continuous transcription mode"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output file for transcriptions (continuous mode only)"
    )
    parser.add_argument(
        "--silence-threshold",
        type=float,
        default=0.01,
        help="RMS threshold below which audio is considered silence (default: 0.01)"
    )

    args = parser.parse_args()

    # List devices and exit if requested
    if args.list_devices:
        list_audio_devices()
        return

    # Show available devices
    list_audio_devices()

    # Auto-detect USB microphone if no device specified
    device_id = args.device
    if device_id is None:
        usb_device = find_usb_microphone()
        if usb_device is not None:
            print(f"\nAuto-selecting USB microphone: Device {usb_device}")
            device_id = usb_device
        else:
            print("\nNo USB microphone detected, using default device")

    # Initialize STT
    print("\nInitializing Speech-to-Text engine...")
    stt = WhisperSTT(model_name=args.model, language=args.language)

    if args.continuous:
        # Continuous mode
        transcriber = ContinuousTranscriber(
            stt=stt,
            chunk_duration=args.duration,
            device_id=device_id,
            output_file=args.output,
            silence_threshold=args.silence_threshold,
        )
        transcriber.run()
    else:
        # Single transcription mode
        transcribe_single(stt, args.duration, device_id)


if __name__ == "__main__":
    main()
