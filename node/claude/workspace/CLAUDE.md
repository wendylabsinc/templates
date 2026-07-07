# Claude Console Workspace

You are running as Claude Code inside a container on a WendyOS device.

## Where you work

- This directory (`/workspace`) is your working area and is persisted across restarts.
- You can read and edit files, run shell commands, and use `git` here.
- You do not have control over the device or other apps — you are sandboxed to this container.

## Operating Rules

- Keep changes small and explain what you did in plain language.
- Prefer editing files in place over large rewrites.
- Keep a short record of important commands and outcomes in `/workspace/notes.md`.
- Do not run destructive commands (broad deletes, `rm -rf` on shared paths) unless the user explicitly asks.
