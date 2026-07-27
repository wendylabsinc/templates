# llm

Local LLM chat app built as a **multi-service app group**: an Ollama model
server and an Open WebUI frontend, each in its own container, defined by a
standard `docker-compose.yml` with a companion `wendy.json`.

```
llm/
├── docker-compose.yml   ← service topology (fully Docker Desktop-compatible)
├── wendy.json           ← companion: appId, GPU entitlement, readiness + postStart hook
├── ollama/              ← Ollama server; pulls the chosen model on first start
├── vllm/                ← vLLM + DFlash serving path (laguna-s-2.1-dflash pick only)
└── open-webui/          ← Open WebUI chat frontend with Wendy branding
```

## The companion pattern

`docker-compose.yml` defines service topology — build contexts, ports,
environment, volumes, and `depends_on`. It contains nothing Wendy-specific
and works as-is with Docker Desktop.

`wendy.json` sits alongside it and adds what compose cannot express:

```jsonc
{
  "appId": "{{.APP_ID}}",
  "services": {
    "ollama": {
      // GPU access is declared here, not in docker-compose.yml
      "entitlements": [{ "type": "gpu" }]
    },
    "open-webui": {}
  },
  // App-level readiness probe + postStart hook: fires once after all
  // services start, and opens the browser at the WebUI.
  "readiness": { "tcpSocket": { "port": {{.PORT}} }, "timeoutSeconds": 180 },
  "hooks": { "postStart": { "cli": "wendy utils open-browser ..." } }
}
```

When you `wendy run`, the CLI merges both files:

- Topology, `ports`, `environment`, named volumes, and `depends_on` come from
  `docker-compose.yml`. Port mappings become `network` entitlements; named
  volumes become `persist` entitlements automatically.
- `appId`, the per-service `gpu` entitlement, and the app-level
  `readiness`/`hooks` come from `wendy.json`.

## Services

| Service | Port | Purpose |
|---------|------|---------|
| `ollama` | 11434 | Ollama API. Pulls the configured model in the background on first start; weights persist in the `…-models` volume. |
| `open-webui` | {{.PORT}} | Chat UI. Persists user data in the `…-openwebui` volume. |

With the `laguna-s-2.1-dflash` pick, the `ollama` service is replaced by
`vllm` (port 8000): an OpenAI-compatible API with DFlash speculative
decoding; weights persist in the `…-hf` volume. See "DFlash mode" below.

## Choosing a model

The model is picked when the template is scaffolded (the `OLLAMA_MODEL`
variable; the full curated picker lives in `template.schema.json`). The
default is `gemma4:e2b`. Rough guidance by device:

| Device | Good picks |
|--------|------------|
| Raspberry Pi 5 | `gemma4:e2b` (slow), `qwen2.5:3b`, `llama3.2:3b` |
| Jetson Orin Nano 8GB | `gemma4:e2b`/`e4b`, `qwen2.5:3b` (~30 tok/s), `llama3.2:3b`, `gemma3:4b`, `mistral:7b` (~15 tok/s), `nemotron-3-nano-4b` |
| Jetson AGX Orin 32/64GB | `gemma4:26b`, `gemma4:31b` (64GB), `nemotron-3-nano:30b`, `qwen3-coder:30b` |
| Jetson AGX Thor (128GB) | `gpt-oss:120b`, `nemotron-3-super:120b`, `laguna-s-2.1` (~16 tok/s), `laguna-s-2.1 (DFlash • vLLM)`, `qwen3-coder:30b` |

To switch models later, edit the `OLLAMA_MODEL` environment value in
`docker-compose.yml` and re-run; the entrypoint pulls whatever it is set to.

## DFlash mode (`laguna-s-2.1-dflash`)

DFlash is lossless speculative decoding: a small block-diffusion draft model
proposes a block of tokens and Laguna verifies the whole block in one forward
pass, so output is identical to running Laguna alone but ~2-4x faster. Ollama
does not support it, so this pick swaps the backend at scaffold time — the
rendered `docker-compose.yml`/`wendy.json` define a `vllm` service (serving
`poolside/Laguna-S-2.1-NVFP4` with the paired DFlash draft model behind an
OpenAI-compatible API) instead of `ollama`, and Open WebUI points at that.

Things to know:

- **Platform requirements are tighter than the Ollama path.** The pinned NGC
  image (26.06) is the earliest whose vLLM natively supports Laguna, but its
  torch is built on CUDA 13.3, and Tegra devices have no CUDA forward
  compatibility — on JetPack 7.2 (CUDA 13.2, e.g. WendyOS 0.18 on Thor) it
  segfaults at CUDA init, and the older CUDA-13.2 image (26.05) predates
  Laguna support entirely. This pick therefore needs a JetPack/driver with
  CUDA 13.3+. PyPI vLLM wheels are not an alternative: they ship no sm_110
  (Thor) kernels.
- DFlash itself additionally needs vLLM >= 0.25.1 (the Laguna DFlash drafter
  from vllm-project/vllm#46853). No NGC tag ships that yet, so today the
  entrypoint's probe logs a `WARNING:` and serves Laguna on plain vLLM —
  everything works, just without the speedup. When a capable NGC tag lands,
  the Dockerfile `FROM` bump is the only change needed. Set
  `DFLASH_DISABLE=1` on the service to force plain serving.
- First start downloads ~72GB of weights into the `…-hf` volume, and unlike
  Ollama's background pull the API stays down until the model is loaded — the
  UI comes up with an empty model list; watch the `[vllm]` log lines.
- Model and quantization are env overrides on the `vllm` service:
  `VLLM_TARGET_MODEL` (e.g. `poolside/Laguna-S-2.1-INT4` if NVFP4 misbehaves
  on your GPU) and `DFLASH_DRAFT_MODEL`.
- The poolside repos are public today. If they become gated, add `HF_TOKEN`
  to the `vllm` service `environment` in `docker-compose.yml` —
  huggingface_hub picks it up automatically.

## Run on a Wendy device

```sh
wendy run
```

Both services build in parallel, start in dependency order, and stream
color-prefixed logs. Then open:

```
http://<device-hostname>:{{.PORT}}
```

The first start downloads the model in the background — watch the `[ollama]`
log lines for pull progress. The model list in the UI populates once the pull
completes. If the device does not have network/DNS when the app first starts,
the Ollama service keeps retrying instead of giving up, so the model appears
automatically once connectivity is restored. If Open WebUI shows an empty model
picker, the model is still downloading or the puller is retrying; check the
Ollama service logs before pulling manually.

> On WendyOS, app groups do not get Docker Compose's service-name DNS, so
> the local Compose URLs (`http://ollama:11434`, `http://vllm:8000`) do not
> resolve from Open WebUI. The entrypoint rewrites whichever is configured to
> its `http://127.0.0.1:<port>` form on device because both backends publish
> their API on the shared device network stack.
> This deliberately avoids the device's `.local` hostname: mDNS works on the
> host for discovery, but app containers do not reliably include the NSS/mDNS
> pieces needed to resolve `.local` names from inside the container.

> App groups support a top-level `readiness` probe and `postStart` hook in
> `wendy.json` as an app-level fallback: it fires once after **all** services
> start, probed against the device host. This template uses it to open the
> browser at the WebUI once the port accepts connections. Requires a Wendy CLI
> with WendyOS PR #1386; older CLIs ignore these keys harmlessly (everything
> works, the browser just isn't opened automatically).

## Run locally with Docker Desktop

`docker-compose.yml` contains no Wendy extensions, so it works unmodified:

```sh
docker compose up
```

Locally the WebUI reaches Ollama at `http://ollama:11434` via Docker's
built-in service-name DNS. In DFlash mode the rendered compose file needs an
NVIDIA GPU host with ~80GB+ of free memory to be useful.

## Useful commands

```sh
wendy run --detach           # start and return; stream later with:
wendy device logs {{.APP_ID}} --service ollama --tail 100
wendy device apps list       # list both containers
```
