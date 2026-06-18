# {{.APP_ID}}

Native macOS Swift LLM backend for [Open WebUI](https://openwebui.com) using
Apple MLX / `mlx-swift-lm`.

This template runs on **Wendy Agent for Mac** (`platform: "darwin"`). It is not a
Linux container template. The app exposes a small OpenAI-compatible HTTP API so
Open WebUI can connect to local inference running on an Apple Silicon Mac.

## What is included

- Swift Package Manager app
- MLX LLM inference through `mlx-swift-lm`
- Hugging Face model download/cache on the target Mac
- OpenAI-compatible endpoints:
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- OpenAI-style streaming responses via Server-Sent Events (the template buffers each MLX completion before emitting it)

The default model is:

```text
{{.MODEL_ID}}
```

It is intentionally small for constrained Macs. You can change it in
`wendy.json` under `run.args` (`--model`) or regenerate the template with
`--var MODEL_ID=...`.

## Run on Wendy Agent for Mac

Install and launch `WendyAgentMac.app` on the target Mac, then run from this
project directory:

```sh
wendy run --device <mac-agent-name>
```

The first chat request may download model weights from Hugging Face on the Mac
agent. Do not commit downloaded model weights or `.build/` artifacts.

When the app starts, logs include the OpenAI-compatible base URL:

```text
MLX_OPENWEBUI_BASE_URL=http://<mac-hostname>:{{.PORT}}/v1
```

## Configure Open WebUI

In Open WebUI, add an OpenAI-compatible connection:

- **Base URL**: `http://<mac-hostname>:{{.PORT}}/v1`
- **API key**: any non-empty value, for example `local-mlx`
- **Model**: `{{.MODEL_ID}}`

If Open WebUI is running on the same Mac as this app, you can use:

```text
http://localhost:{{.PORT}}/v1
```

## Local development

Build the native app on Apple Silicon macOS:

```sh
swift build
```

Run without Wendy:

```sh
swift run {{.APP_ID}} --host 127.0.0.1 --port {{.PORT}} --model {{.MODEL_ID}}
```

Smoke-test the API:

```sh
curl http://127.0.0.1:{{.PORT}}/v1/models
curl http://127.0.0.1:{{.PORT}}/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"{{.MODEL_ID}}","messages":[{"role":"user","content":"Say hello in one sentence."}],"stream":false}'
```

## Choosing a model

Use MLX-format models from Hugging Face, commonly under `mlx-community/`. Some
small options to try:

- `mlx-community/Qwen1.5-0.5B-Chat-4bit` — lightweight default
- `mlx-community/SmolLM-135M-Instruct-4bit` — very small validation model
- `mlx-community/Llama-3.2-1B-Instruct-4bit` — better quality, more memory

Larger models need more unified memory and disk space.
