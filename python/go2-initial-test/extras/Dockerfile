# syntax=docker/dockerfile:1.6
# go2-initial-test — "extras" reporter: ultrasonic, body LEDs, camera gimbal.
# No known DDS topic / SDK API exists for these in the Go2 demos, so this
# service reports them honestly as `na` (unverified) rather than faking a result.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY app.py ./

ENV PORT=3618
EXPOSE 3618

CMD ["python", "app.py"]
