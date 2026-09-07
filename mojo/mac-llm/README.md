# {{.APP_ID}}

Browser chat on Apple Silicon, using MAX **26.5.0** with Python **3.14** and
Open WebUI **0.9.5** with Python **3.11**. A single Python launcher installs and
supervises both services in separate environments using `uv` on the target Mac.

## Requirements

- Apple Silicon Mac with Wendy Agent advertising `native-process-v1`
- Updated Wendy CLI, Homebrew, and Xcode Command Line Tools on the target
- Internet access and enough free disk and unified memory for the model

`Brewfile.wendy` installs `uv`. No local build is needed. MAX uses the Metal GPU;
its supported model families vary. Start with the verified small default model.

## Run and verify

```sh
wendy run
```

The browser opens at `http://<mac-hostname>:{{.PORT}}` after readiness succeeds.
Create the first WebUI account and send a short prompt. MAX binds only to
`127.0.0.1:11435`; browser chat listens on port `{{.PORT}}` on the Mac's interfaces.
WebUI starts after MAX's `/health` succeeds. First startup downloads Python,
packages, and model weights and compiles the model. The readiness budget is
600 seconds; an attached CLI continues observing a running app after that.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `APP_ID` | `{{.APP_ID}}` | App identity and runtime directory |
| `PORT` | `8080` | Browser chat port selected when scaffolding |
| `MAX_MODEL` | `HuggingFaceTB/SmolLM2-135M-Instruct` | Hugging Face model |

Override the model with `wendy run --env MAX_MODEL=Qwen/Qwen2.5-1.5B-Instruct` or
edit `wendy.json`. MAX uses batch size 1, context length 2048, and device memory
utilization 0.2. These limits avoid sizing the workload against all unified
memory. The small default model is intended for deployment verification; use a
larger supported model for better chat quality.

## How it works

`run.command` invokes `/usr/bin/python3 launcher.py` in the synced app directory.
The launcher runs `uv` to create separate MAX and WebUI environments, forwards
their output, and stops both process groups on shutdown. If either service
exits, the app fails so the agent's restart policy can recover it. A runtime
lock prevents two launchers from owning the same app data.

## Extend it

Edit `launcher.py` to adjust MAX limits or WebUI configuration. Add files through
`wendy.json`. Native service groups and a setup API are not required by this
template; setup is part of the supervised launch.

## Operations and troubleshooting

All environments, Python downloads, model and compile caches, WebUI data, and
the WebUI secret key are retained across redeploys and restarts under:

```text
~/Library/Application Support/{{.APP_ID}}/runtime/
```

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

Warm starts reuse installed packages and caches. To reset data, stop the app
first, then remove only the desired runtime subdirectory. Check setup logs for
download failures, MAX logs for unsupported models or Metal compilation errors,
and port conflicts if health does not succeed. A stopped child is reported as a
failure and causes its sibling to be terminated.
