# {{.APP_ID}}

A native Apple Silicon chat application for Wendy Agent for Mac. A Swift MLX
backend exposes a private OpenAI-compatible API to Open WebUI, which provides
the browser interface.

## Requirements

- An Apple Silicon Mac running Wendy Agent for Mac
- Wendy CLI access to that Mac
- Xcode with the Swift toolchain needed by the project
- Homebrew; `Brewfile.wendy` installs `uv` on the target
- Internet access and enough disk for Open WebUI and the selected Hugging Face
  MLX model
- Unified memory suitable for the selected model

This is a native `darwin` project, not a Linux container. The first run installs
Open WebUI and downloads the model before readiness succeeds.

## Run and verify

```sh
wendy run
```

Open `http://<mac-hostname>:{{.PORT}}`, create or open a chat, and send a short
prompt. The WebUI is exposed on the Mac's network interfaces. The MLX `/v1` API
listens only on `127.0.0.1:{{.MLX_PORT}}` and uses an app-generated bearer token.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application name and local data-directory name |
| `PORT` | `8080` | Public Open WebUI port and readiness probe |
| `MLX_PORT` | `11435` | Private localhost MLX API port |
| `MODEL_ID` | `mlx-community/Qwen2.5-3B-Instruct-4bit` | Hugging Face MLX model |
| `OPEN_WEBUI_VERSION` | `0.9.5` | Open WebUI package installed with `uv` |
| `MAX_TOKENS` | `256` | Default completion token limit |

Use MLX-format models and start with a small 4-bit model. Larger models require
more download space and unified memory. To change a generated project, edit the
arguments in `wendy.json` and run it again.

## How it works

`wendy run` builds the `MacLLM` Xcode scheme so MLX Metal resources are
available. `Sources/{{.APP_ID}}/App.swift` installs the app-local Python tools,
prefetches the model, starts the Swift MLX server, starts Open WebUI, connects
the two, and shuts down child processes with the supervisor.

Application data is stored under:

```text
~/Library/Application Support/{{.APP_ID}}/
```

Model files use `HF_HUB_CACHE` or `HF_HOME` when set, otherwise the normal
Hugging Face cache under `~/.cache/huggingface/hub/`.

## Extend it

- Add or change OpenAI-compatible routes in `App.swift`.
- Add supervisor arguments in `CLIOptions` and the matching `wendy.json`
  `run.args` entry.
- Change Open WebUI setup in `OpenWebUIRuntime` and dependency installation in
  `Brewfile.wendy`.
- Set `HF_TOKEN` in the target environment for gated models.

For local development:

```sh
brew install uv
xcodebuild -project MacLLM.xcodeproj -scheme MacLLM \
  -configuration Release -derivedDataPath .xcode \
  -skipMacroValidation -skipPackagePluginValidation
.xcode/Build/Products/Release/MacLLM \
  --webui-host 127.0.0.1 --webui-port {{.PORT}} \
  --mlx-host 127.0.0.1 --mlx-port {{.MLX_PORT}} \
  --model {{.MODEL_ID}}
```

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

If startup is slow, check whether `uv`, Open WebUI, or the model is still being
installed. If loading fails, confirm the model is MLX-compatible and fits in
available disk and unified memory. Stop or change any process already using
either configured port.
