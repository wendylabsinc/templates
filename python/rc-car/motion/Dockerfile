# rc-car motion API — wraps Yahboom Rosmaster_Lib over /dev/ttyUSB0.
#
# Runs with `network: host` (so the rc UI reaches it on 127.0.0.1:3201) and the
# `serial` entitlement for ttyUSB0 (the STM32 driver board).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Vendored Yahboom driver library (not published on PyPI).
COPY Rosmaster_Lib.py app.py ./

ENV PORT=3201
# Override to open up the speed/steer envelope or point at a different node.
ENV ROSMASTER_COM=auto
ENV MAX_SPEED=0.6
ENV MAX_STEER_DEG=30

EXPOSE 3201

CMD ["python", "app.py"]
