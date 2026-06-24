# rc-car joystick — reads a USB/Bluetooth gamepad and drives via the motion API.
#
# Runs with `network: host` (reach motion on 127.0.0.1:3201), `input`
# (read /dev/input/js*), and `bluetooth` (let a BT gamepad pair/connect).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENV PORT=3600
ENV JOYSTICK_DEV=/dev/input/js0
ENV MOTION_URL=http://127.0.0.1:3201
# Gamepad axis mapping (override per controller — see /health for live axes).
ENV THROTTLE_AXIS=1
ENV STEER_AXIS=0
ENV THROTTLE_SIGN=-1

EXPOSE 3600

CMD ["python", "app.py"]
