# {{.APP_ID}}

A lean **Claude Code console** for a WendyOS device. It runs the Claude Code CLI
inside a container as an unprivileged user and serves a token-gated HTTPS page
that accepts text prompts and browser-transcribed voice prompts, then streams
the Claude Code run back to the page. All work happens in a persisted
`/workspace`.

Unlike the `hermes-agent` template, this template has **no device control**: no
`admin` or `build` entitlement, no Wendy MCP, and no on-device BuildKit. It is a
sandboxed coding assistant, not a device operator.

## Scaffold

```bash
wendy init \
  --app-id claude \
  --target wendyos \
  --language node \
  --template claude \
  --var CONSOLE_TOKEN="$(openssl rand -hex 24)"
```

`CONSOLE_TOKEN` gates the browser console — keep it private. `PORT` defaults to
`3091` and `CLAUDE_COMMAND` defaults to `claude --print --dangerously-skip-permissions`.

## Deploy

```bash
wendy run .
```

The app opens `https://${WENDY_HOSTNAME}:3091` (or your chosen `PORT`). The first
load uses a self-signed certificate generated in `/state/tls`; accept the
browser warning once, then enter the console token you configured while
scaffolding.

## Log in to Claude Code (one time)

Claude Code runs as the unprivileged `claude` user, and its login is stored
under `/home/claude`, which `wendy.json` persists. The `Claude auth` badge in
the console turns amber until you have logged in. Attach once and complete the
OAuth flow as that user:

```bash
wendy device attach {{.APP_ID}} -- claude-user claude
```

After OAuth completes, return to the web console and send prompts from the text
box or the `Mic` button. Browser speech support depends on the browser;
Chrome-based browsers are the most reliable.

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
edit anything under `/workspace`. Keep the token private and avoid exposing the
port beyond your trusted network. The container has host networking (so Claude
Code can reach the Anthropic API) but no access to the Wendy admin socket or
other apps.

## Persistence

`wendy.json` persists three volumes:

| Path | Holds |
|------|-------|
| `/home/claude` | Claude Code login and settings |
| `/workspace` | Your files (seeded once with `CLAUDE.md`) |
| `/state` | TLS certificate and prompt history (`claude-history.ndjson`) |
