# rc-car teleop UI — serves the control page and proxies motion + camera.
#
# Runs with `network: host` so it reaches motion (127.0.0.1:3201) and camera
# (127.0.0.1:8000) as siblings.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY static ./static

ENV PORT={{.RC_PORT}}
ENV MOTION_URL=http://127.0.0.1:3201
ENV CAMERA_URL=http://127.0.0.1:8000/stream/color

EXPOSE {{.RC_PORT}}

CMD ["python", "main.py"]
