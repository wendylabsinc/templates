# {{.APP_ID}}

Native macOS LLM chat app for **Wendy Agent for Mac**. It runs:

- a Swift / Apple MLX OpenAI-compatible model backend on localhost
- Open WebUI as the browser UI

This template targets `platform: "darwin"`. It is not a Linux container template;
the model backend runs as a native macOS process so Apple Silicon can use MLX,
Metal, and unified memory.

## How it works

`wendy run` builds this Xcode project with `xcodebuild` and deploys the native Swift supervisor to the target Mac. The Xcode build is intentional: MLX Swift requires compiled Metal shader resources from the `Cmlx` package, and upstream MLX documents that “SwiftPM (command line) cannot build the Metal shaders so the ultimate build has to be done via Xcode.”

On startup the supervisor:

1. applies `Brewfile.wendy` on the target Mac, installing `uv`
2. installs pinned Open WebUI (`{{.OPEN_WEBUI_VERSION}}`) app-locally with `uv`
3. downloads/loads the configured MLX model with the Swift Hugging Face client
4. starts a private MLX OpenAI-compatible API on `127.0.0.1:{{.MLX_PORT}}`
5. starts Open WebUI on `0.0.0.0:{{.PORT}}`
6. configures Open WebUI to talk to the private MLX API with an app-generated API key

App-local runtime data is stored on the target Mac under:

```text
~/Library/Application Support/{{.APP_ID}}/
```

Model weights use the user-wide Hugging Face cache, honoring `HF_HUB_CACHE` /
`HF_HOME` when set and otherwise defaulting to:

```text
~/.cache/huggingface/hub/
```

Models are downloaded during app startup, before Open WebUI is marked ready. Do
not commit model weights, Open WebUI data, `.build/`, or `.xcode/` artifacts.

## Run on a headless Mac agent

Install and launch `WendyAgentMac.app` on the target Mac, then run from this
project directory:

```sh
wendy run --device <mac-agent-name>
```

The first run can take a few minutes while Homebrew installs `uv`, `uv` installs
Open WebUI, and the Swift MLX backend downloads/loads the configured model from
Hugging Face.

When startup completes, open:

```text
http://<mac-hostname>:{{.PORT}}
```

For example:

```text
http://mac-mini.local:{{.PORT}}
```

Open WebUI is exposed on the LAN. The raw MLX `/v1` API is bound to localhost
only and protected with a generated bearer token used internally by Open WebUI.

## Configuration

The default model is:

```text
{{.MODEL_ID}}
```

Change it in `wendy.json` under `run.args` (`--model`) or regenerate with:

```sh
wendy init --target darwin --template mac-llm --var MODEL_ID=mlx-community/Qwen2.5-3B-Instruct-4bit
```

Useful options in `wendy.json`:

- `--webui-port {{.PORT}}` — public Open WebUI port
- `--mlx-port {{.MLX_PORT}}` — private localhost MLX API port
- `--open-webui-version {{.OPEN_WEBUI_VERSION}}` — pinned Open WebUI package
- `--default-max-tokens {{.MAX_TOKENS}}` — fallback generation length

## Choosing a model

Use MLX-format models from Hugging Face, commonly under `mlx-community/`.
Smaller 4-bit models are best for initial validation; larger models benefit from
Macs with more unified memory.

Examples:

- `mlx-community/SmolLM-135M-Instruct-4bit` — very small smoke-test model
- `mlx-community/Llama-3.2-1B-Instruct-4bit` — lightweight default
- `mlx-community/Qwen2.5-3B-Instruct-4bit` — better quality, more memory
- `mlx-community/Qwen2.5-7B-Instruct-4bit` — larger model for higher-memory Macs

## Local development

Build on Apple Silicon macOS:

```sh
xcodebuild \
  -project MacLLM.xcodeproj \
  -scheme MacLLM \
  -configuration Release \
  -derivedDataPath .xcode \
  -skipMacroValidation \
  -skipPackagePluginValidation
```

Run locally without Wendy:

```sh
.xcode/Build/Products/Release/MacLLM \
  --webui-host 127.0.0.1 \
  --webui-port {{.PORT}} \
  --mlx-host 127.0.0.1 \
  --mlx-port {{.MLX_PORT}} \
  --model {{.MODEL_ID}}
```

This still requires `uv` on your development Mac:

```sh
brew install uv
```

No Hugging Face CLI is required. The Swift app links the Hugging Face client
library at build time. For private or gated models, pass `HF_TOKEN` in the app
environment on the target Mac. To use a custom shared model cache, set
`HF_HUB_CACHE` or `HF_HOME` before launching the app.
