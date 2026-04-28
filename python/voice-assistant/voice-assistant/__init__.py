"""
{{.APP_ID}} — a {{.PROJECT_KIND}} powered by voice
"""

import os
import signal
import sys


def _shutdown(sig, frame):
    print("Shutting down gracefully...")
    sys.exit(0)


{{if eq .INFERENCE_MODE "local"}}
def _load_model():
{{if eq .LOCAL_MODEL "vosk"}}
    from vosk import Model, KaldiRecognizer
    model_path = os.environ.get("VOSK_MODEL_PATH", "model")
    return Model(model_path), KaldiRecognizer
{{else}}
    import whisper
    model_name = os.environ.get("LOCAL_MODEL", "{{.LOCAL_MODEL}}").replace("whisper_", "")
    return whisper.load_model(model_name), None
{{end}}
{{end}}
{{if eq .INFERENCE_MODE "cloud"}}
def _make_client():
{{if eq .CLOUD_PROVIDER "openai"}}
    from openai import OpenAI
    return OpenAI(api_key=os.environ["API_TOKEN"])
{{else if eq .CLOUD_PROVIDER "anthropic"}}
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["API_TOKEN"])
{{else if eq .CLOUD_PROVIDER "deepgram"}}
    from deepgram import DeepgramClient
    return DeepgramClient(os.environ["API_TOKEN"])
{{else}}
    # google
    from google.cloud import speech
    return speech.SpeechClient()
{{end}}
{{end}}


def main():
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("Starting {{.APP_ID}} ({{.PROJECT_KIND}})...")
    port = int(os.environ.get("PORT", "{{.PORT}}"))

{{if eq .INFERENCE_MODE "local"}}
    model, recognizer_cls = _load_model()
    print(f"Local model ready. Listening on port {port}.")
{{end}}
{{if eq .INFERENCE_MODE "cloud"}}
    client = _make_client()
    print(f"Cloud provider: {{.CLOUD_PROVIDER}}. Listening on port {port}.")
{{end}}

    # TODO: implement audio capture and transcription loop here
    import time
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
