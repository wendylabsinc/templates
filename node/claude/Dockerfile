# syntax=docker/dockerfile:1.6
# A lean Claude Code console: Claude Code runs inside a WendyOS container as an
# unprivileged user and a small HTTPS server exposes a browser page for text or
# browser-transcribed voice prompts, pointed at a persisted /workspace.
FROM node:22-bookworm-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    less \
    openssl \
    procps \
    ripgrep \
    util-linux \
  && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. Authentication is persisted in /home/claude by wendy.json.
RUN --mount=type=cache,target=/root/.npm npm install -g @anthropic-ai/claude-code
RUN useradd --create-home --shell /bin/bash claude

WORKDIR /app
COPY package.json ./
COPY server.mjs ./
COPY public ./public
COPY workspace /opt/claude-workspace
COPY claude-user.sh /usr/local/bin/claude-user
COPY init.sh /usr/local/bin/claude-console-init
RUN chmod +x /usr/local/bin/claude-console-init /usr/local/bin/claude-user

ENV NODE_ENV=production
ENV PORT={{.PORT}}
ENV CONSOLE_TOKEN="{{.CONSOLE_TOKEN}}"
ENV CLAUDE_COMMAND="{{.CLAUDE_COMMAND}}"
ENV HTTPS_CERT_PATH=/state/tls/cert.pem
ENV HTTPS_KEY_PATH=/state/tls/key.pem
# Claude subscription token from `claude setup-token`, baked in at `wendy init`
# so Claude Code is authenticated without an in-container OAuth login.
ENV CLAUDE_CODE_OAUTH_TOKEN="{{.CLAUDE_CODE_OAUTH_TOKEN}}"

WORKDIR /workspace
EXPOSE {{.PORT}}

CMD ["/usr/local/bin/claude-console-init"]
