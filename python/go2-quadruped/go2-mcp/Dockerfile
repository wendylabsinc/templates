# go2-mcp deployable image. SSE transport on :3400 so voice-AI (running
# in a sibling container on the same Jetson with network: host) can
# reach the tools by URL.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install the package from source. pyproject.toml uses hatchling and the
# entrypoint script `go2-mcp` is what we'll invoke from CMD.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Container deploy uses SSE on a TCP port; voice-AI connects via URL.
# Motion + brain are localhost on the Jetson because watchtower/brain/
# motion all use Wendy's network: host entitlement.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3400 \
    GO2_MOTION_URL=http://127.0.0.1:3201 \
    GO2_BRAIN_URL=http://127.0.0.1:3300

EXPOSE 3400

ENTRYPOINT ["go2-mcp"]
