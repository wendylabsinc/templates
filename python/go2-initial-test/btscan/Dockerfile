# syntax=docker/dockerfile:1.6
# go2-initial-test — active Bluetooth scan/pair test (exercises the `bluetooth`
# entitlement, unlike the presence-only `bt` tile).
#
# bleak talks to BlueZ over D-Bus (org.bluez) via pure-python dbus-fast — no
# bluez binary needed in the image. It DOES require the `bluetooth` entitlement
# (mounts the xdg-dbus-proxy socket + sets DBUS_SYSTEM_BUS_ADDRESS). If that
# entitlement's proxy dir isn't created by the agent, this container may fail to
# start — which is itself the signal that the bluetooth entitlement is broken.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY app.py ./

ENV PORT=3622
ENV BLE_SCAN_SECONDS=5
# Optional: set to a MAC to also attempt a connect/pair to that device.
ENV BT_PAIR_TARGET=
EXPOSE 3622

CMD ["python", "app.py"]
