# Central component — the camera-wall dashboard. Stdlib-only Python, so a bare
# slim image is all it needs. The platform places this once (on a hub device, or
# in the cloud) per the `target: { central: true }` in ../wendy.json, and injects
# WENDY_FLEET_PEERS + WENDY_DISCOVERY_URL so it auto-populates from the fleet.
#
# (It also runs host-side with no container at all: `python3 serve.py`.)
FROM python:3.11-slim

WORKDIR /app
COPY serve.py index.html ./

ENV PYTHONUNBUFFERED=1 \
    DASHBOARD_PORT={{.DASHBOARD_PORT}}

EXPOSE {{.DASHBOARD_PORT}}
CMD ["python", "serve.py"]
