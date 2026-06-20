# go2-initial-test — microphone test (record from the Jetson's local input).
# Uses the Jetson's ALSA input (sounddevice) rather than the WebRTC head mic, so
# it doesn't contend for the single WebRTC slot the camera test uses.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

ENV PORT=3614
# Set MIC_DEVICE=<int> to pick a non-default input (list with `python3 -m sounddevice`).
ENV MIC_DEVICE=
ENV SAMPLE_RATE=16000
ENV RECORD_SECONDS=3
EXPOSE 3614

CMD ["python", "app.py"]
