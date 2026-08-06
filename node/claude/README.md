# {{.APP_ID}}

A **Claude Code console** that runs Claude Code on a WendyOS device with full
control of the device. It packages the Wendy CLI, Wendy MCP setup, and an
in-container BuildKit daemon, and serves a token-gated HTTPS page that accepts
text prompts and browser-transcribed voice prompts, then streams the Claude Code
run back to the page. Prompts can inspect the device, edit projects under
`/workspace`, build with `wendy run --yes`, and deploy apps through the local
admin socket.

It is functionally the same on-device agent as `hermes-agent`, with one
difference: the Claude subscription token is baked in at `wendy init` (see
below), so the device never has to run the interactive OAuth login that is
unreliable in headless / attach sessions.

## Scaffold

Claude Code is authenticated at scaffold time so the device never has to run an
interactive OAuth login (which is unreliable in headless / attach sessions).
First, on a machine with a browser, mint a subscription token:

```bash
claude setup-token          # completes OAuth locally, prints sk-ant-oat...
```

Then scaffold, passing that token (and a console token):

```bash
wendy init \
  --app-id claude \
  --target wendyos \
  --language node \
  --template claude \
  --var CONSOLE_TOKEN="$(openssl rand -hex 24)" \
  --var CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat..."
```

- `CONSOLE_TOKEN` gates the browser console — keep it private.
- `CLAUDE_CODE_OAUTH_TOKEN` (required) authenticates Claude Code via your
  subscription. It is baked into the image `ENV`.
- `PORT` defaults to `3091`; `CLAUDE_COMMAND` defaults to
  `claude --print --dangerously-skip-permissions`.

Because the credential is baked into the image layers and the rendered
`Dockerfile`, treat the scaffolded project and its image as secret — don't push
the image to a public registry.

## Wendy CLI

The Dockerfile installs the Wendy CLI during the image build via the public Wendy
installer. By default it installs the latest release. For direct Docker builds or
CI, pass `--build-arg WENDY_INSTALL_URL=...` to use an internal mirror.

## Deploy

```bash
wendy run .
```

The app opens `https://${WENDY_HOSTNAME}:3091` (or your chosen `PORT`). The first
load uses a self-signed certificate generated in `/state/tls`; accept the
browser warning once, then enter the console token you configured while
scaffolding.

## Authentication

Claude Code is authenticated by the `CLAUDE_CODE_OAUTH_TOKEN` baked in at scaffold
time — nothing else to do. Send prompts from the text box or the `Mic` button.
Browser speech support depends on the browser; Chrome-based browsers are the
most reliable.

### Re-authenticating

If the token is revoked or expires, mint a new one with `claude setup-token` and
re-scaffold (or rebuild with a new `--var CLAUDE_CODE_OAUTH_TOKEN=...`). Avoid
the interactive `claude` login inside an attach/SSH session: the OAuth URL it
prints is often truncated by terminal line wrapping, which yields an
`Invalid OAuth Request — Unknown scope` error.

## Using the console

Type or speak an instruction and press `Send`. Claude Code runs once per prompt
(`--print`) with `/workspace` as its working directory and streams stdout/stderr
into the log. A good first prompt:

```text
Create a WendyOS simple-api app under /workspace/apps/hello-api, deploy it, and show me the health endpoint.
```

- **Auto send** submits a voice prompt as soon as speech recognition finalizes.
- **Speak** reads the tail of each response aloud.
- **Stop** cancels the running job.
- The **Workspace** panel lists the top-level entries in `/workspace`.

## Building Apps on the Device

`/workspace/CLAUDE.md` seeds operating rules for Claude Code. On-device builds use
`buildkitd` through:

```bash
BUILDKIT_HOST=unix:///run/buildkit/buildkitd.sock
```

If BuildKit fails with overlayfs errors on your device kernel, set
`BUILDKIT_SNAPSHOTTER=native` in the container environment and redeploy.

## Security

This app has `admin` and `build` entitlements. It can control apps, read device
telemetry, exec into containers, build images, deploy apps, and use privileged
builder kernel features — and anyone with the console token can drive it. Deploy
only to trusted first-party devices, keep the console token private, and avoid
exposing the port beyond your trusted network. The container also has host
networking so Claude Code can reach the Anthropic API.

## Persistence

`wendy.json` persists four volumes:

| Path | Holds |
|------|-------|
| `/home/claude` | Claude Code login and settings |
| `/workspace` | Your files (seeded once with `CLAUDE.md`) |
| `/state` | TLS certificate and prompt history (`claude-history.ndjson`) |
| `/var/lib/buildkit` | BuildKit layer cache |
