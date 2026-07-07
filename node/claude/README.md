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

The recommended path authenticates Claude Code at scaffold time so the device
never has to run an interactive OAuth login (which is unreliable in headless /
attach sessions). First, on a machine with a browser, mint a subscription token:

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
- `CLAUDE_CODE_OAUTH_TOKEN` (optional) authenticates Claude Code via your
  subscription. Use `ANTHROPIC_API_KEY` instead for API billing. Either is baked
  into the image `ENV`; leave both blank to log in later via attach.
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

If you passed `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) at scaffold
time, Claude Code is already authenticated — nothing else to do. Send prompts
from the text box or the `Mic` button. Browser speech support depends on the
browser; Chrome-based browsers are the most reliable.

### Fallback: log in via attach

If you left both credentials blank, log in once interactively. Claude Code runs
as the unprivileged `claude` user, and its login is stored under `/home/claude`,
which `wendy.json` persists. The `Claude auth` badge in the console stays amber
until you have logged in:

```bash
wendy device attach {{.APP_ID}} -- claude-user claude
```

Note: the OAuth URL that `claude` prints can be truncated by terminal line
wrapping in an attach/SSH session, which yields an `Invalid OAuth Request —
Unknown scope` error. Copy the entire URL (widen the terminal first), or use the
`claude setup-token` path above instead.

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
