# {{.APP_ID}}

Hermes Agent runs Claude Code on a WendyOS device with the Wendy admin socket
and an in-container BuildKit daemon. The browser console accepts text prompts
and browser-transcribed voice prompts, then streams the Claude Code run back to
the page.

## Security

This app has `admin` and `build` entitlements. It can control apps, read device
telemetry, exec into containers, build images, deploy apps, and use privileged
builder kernel features. Deploy only to trusted first-party devices, keep the
Hermes token private, and avoid exposing port `{{.PORT}}` beyond your trusted
network.

## Stage the Wendy CLI

The image expects an arm64 Wendy CLI in the build context as `wendy-linux-arm64`.
From the Wendy repo root:

```bash
GOOS=linux GOARCH=arm64 go build -o /path/to/{{.APP_ID}}/wendy-linux-arm64 ./go/cmd/wendy
```

## Deploy

```bash
wendy run .
```

The app opens `https://${WENDY_HOSTNAME}:{{.PORT}}`. The first load uses a
self-signed certificate generated in `/state/tls`; accept the browser warning
once, then enter the Hermes token you configured while scaffolding the template.

## Claude Code Login

Claude Code stores its login under `/root`, which is persisted by `wendy.json`.
If the web console reports that Claude auth is missing, attach once and log in:

```bash
wendy device attach {{.APP_ID}} -- claude
```

After OAuth completes, return to the web console and send prompts from text or
the `Mic` button. Browser speech support depends on the browser; Chrome-based
browsers are the most reliable.

## Building Apps on the Device

Hermes seeds `/workspace/CLAUDE.md` with operating rules for Claude Code. A good
first prompt is:

```text
Create a WendyOS simple-api app under /workspace/apps/hello-api, deploy it, and show me the health endpoint.
```

On-device builds use `buildkitd` through:

```bash
BUILDKIT_HOST=unix:///run/buildkit/buildkitd.sock
```

If BuildKit fails with overlayfs errors on your device kernel, set
`BUILDKIT_SNAPSHOTTER=native` in the container environment and redeploy.
