# syntax=docker/dockerfile:1.6
# FastMCP-over-HTTP bridge to the sibling go2-motion service. Pure-Python,
# no DDS/SDK here (that all lives in the motion container), so this stays a
# lean slim image that builds in ~1 min.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY server.py .

# Where the sibling go2-motion service lives. Default assumes host networking
# on the dog (both containers on 127.0.0.1). Override to target a remote dog.
ENV MOTION_URL=http://127.0.0.1:3201
ENV MCP_PORT={{.MCP_PORT}}

EXPOSE {{.MCP_PORT}}

CMD ["python", "server.py"]
