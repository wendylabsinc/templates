# Claude Console Workspace

You are running as Claude Code inside a container on a WendyOS device, with full
control of the device through the local Wendy admin socket.

## Device Control

- The `wendy` CLI is installed and already points at the local agent socket through `WENDY_AGENT_SOCKET`.
- Use `wendy device info`, `wendy device apps`, and `wendy device telemetry logs <app>` to inspect the device.
- Use `wendy device attach <app> -- <command>` only when you need to inspect another app container.

## Building Apps

- Keep generated app projects under `/workspace/apps/<name>`.
- Use Wendy templates when possible, for example `wendy init --template simple-api --language node --app-id <app-id>`.
- From an app directory, run `wendy run --yes` to build and deploy on the device.
- On-device builds use BuildKit through `BUILDKIT_HOST=unix:///run/buildkit/buildkitd.sock`.

## Operating Rules

- Treat this device as production hardware: avoid broad deletes, OS updates, or app removal unless explicitly requested.
- Keep a short record of important commands and outcomes in `/workspace/notes.md`.
- Prefer small, deployable iterations over large rewrites.
