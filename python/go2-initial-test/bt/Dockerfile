# syntax=docker/dockerfile:1.6
# go2-initial-test — Bluetooth presence test.
#
# Detects a Bluetooth adapter / USB dongle on the dog's Jetson at the OS level
# (sysfs), so it works WITHOUT the `bluetooth` entitlement — that entitlement
# mounts an xdg-dbus-proxy dir the agent doesn't always create, which can stop a
# container from starting. Reading /sys needs no entitlement, so this always
# starts and reports green (dongle present) / red (none).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY app.py ./

ENV PORT=3621
EXPOSE 3621

CMD ["python", "app.py"]
