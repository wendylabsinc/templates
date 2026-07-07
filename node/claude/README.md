# {{.APP_ID}}

A lean **Claude Code console** for a WendyOS device. It runs the Claude Code CLI
inside a container as an unprivileged user and serves a token-gated HTTPS page
that accepts text prompts and browser-transcribed voice prompts, then streams
the Claude Code run back to the page. All work happens in a persisted
`/workspace`.

This template holds the `admin` and `build` entitlements, so the container can
reach the Wendy admin socket and use privileged builder features. Unlike
`hermes-agent`, it does not bundle the Wendy CLI, Wendy MCP setup, or an
in-container BuildKit daemon — Claude Code works in `/workspace` out of the box,
and you can install and wire up device tooling yourself if you want to use those
entitlements.

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
Create /workspace/hello.py that prints the current time, then run it and show me the output.
```

- **Auto send** submits a voice prompt as soon as speech recognition finalizes.
- **Speak** reads the tail of each response aloud.
- **Stop** cancels the running job.
- The **Workspace** panel lists the top-level entries in `/workspace`.

## Security

Anyone with the console token can run Claude Code in this container and read or
edit anything under `/workspace`. This app holds the `admin` and `build`
entitlements, so with the right tooling it can control apps, read device
telemetry, and use privileged builder features. Keep the console token private,
deploy only to trusted first-party devices, and avoid exposing the port beyond
your trusted network. The container also has host networking so Claude Code can
reach the Anthropic API.

## Persistence

`wendy.json` persists three volumes:

| Path | Holds |
|------|-------|
| `/home/claude` | Claude Code login and settings |
| `/workspace` | Your files (seeded once with `CLAUDE.md`) |
| `/state` | TLS certificate and prompt history (`claude-history.ndjson`) |
