# syntax=docker/dockerfile:1.6
# Hermes Agent runs Claude Code inside an admin-entitled WendyOS container and
# exposes a small HTTPS console for text or browser-transcribed voice prompts.
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
  && rm -rf /var/lib/apt/lists/*

# BuildKit (buildkitd + buildctl + buildkit-runc) for on-device image builds.
# The default target is arm64 because WendyOS devices are normally Jetson/ARM64.
ARG BUILDKIT_VERSION=v0.16.0
ARG TARGETARCH=arm64
RUN case "${TARGETARCH}" in \
      arm64) buildkit_arch="arm64" ;; \
      amd64) buildkit_arch="amd64" ;; \
      *) echo "unsupported BuildKit arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
  && curl -fsSL "https://github.com/moby/buildkit/releases/download/${BUILDKIT_VERSION}/buildkit-${BUILDKIT_VERSION}.linux-${buildkit_arch}.tar.gz" \
      | tar -xz -C /usr/local bin/buildkitd bin/buildctl bin/buildkit-runc \
  && test -x /usr/local/bin/buildctl \
  && test -x /usr/local/bin/buildkitd
ENV BUILDKIT_HOST=unix:///run/buildkit/buildkitd.sock

# Claude Code CLI. Authentication is persisted in /root by wendy.json.
RUN --mount=type=cache,target=/root/.npm npm install -g @anthropic-ai/claude-code

# Stage an arm64 Wendy CLI into this directory before building. See README.md.
COPY wendy-linux-arm64 /usr/local/bin/wendy
RUN chmod +x /usr/local/bin/wendy

WORKDIR /app
COPY package.json ./
COPY server.mjs ./
COPY public ./public
COPY workspace /opt/hermes-workspace
COPY init.sh /usr/local/bin/hermes-agent-init
RUN chmod +x /usr/local/bin/hermes-agent-init

ENV NODE_ENV=production
ENV PORT={{.PORT}}
ENV HERMES_TOKEN="{{.HERMES_TOKEN}}"
ENV CLAUDE_COMMAND="{{.CLAUDE_COMMAND}}"
ENV HTTPS_CERT_PATH=/state/tls/cert.pem
ENV HTTPS_KEY_PATH=/state/tls/key.pem

WORKDIR /workspace
EXPOSE {{.PORT}}

CMD ["/usr/local/bin/hermes-agent-init"]
