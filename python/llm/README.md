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
├── max/                 ← Modular MAX serving path (laguna-s-2.1-max pick only)
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

Two picks replace the `ollama` service with an OpenAI-compatible backend on
port 8000, weights persisting in the `…-hf` volume: `laguna-s-2.1-dflash`
brings up `vllm` (DFlash speculative decoding — see "DFlash mode" below), and
`laguna-s-2.1-max` brings up `max` (Modular MAX — see "MAX mode" below).

## Choosing a model

The model is picked when the template is scaffolded (the `OLLAMA_MODEL`
variable; the full curated picker lives in `template.schema.json`). The
default is `gemma4:e2b`. Rough guidance by device:

| Device | Good picks |
|--------|------------|
| Raspberry Pi 5 | `gemma4:e2b` (slow), `qwen2.5:3b`, `llama3.2:3b` |
| Jetson Orin Nano 8GB | `gemma4:e2b`/`e4b`, `qwen2.5:3b` (~30 tok/s), `llama3.2:3b`, `gemma3:4b`, `mistral:7b` (~15 tok/s), `nemotron-3-nano-4b` |
| Jetson AGX Orin 32/64GB | `gemma4:26b`, `gemma4:31b` (64GB), `nemotron-3-nano:30b`, `qwen3-coder:30b`, `laguna-xs-2.1` (20GB download) |
| Jetson AGX Thor (128GB) | `gpt-oss:120b`, `nemotron-3-super:120b`, `laguna-xs-2.1` (~59 tok/s), `laguna-s-2.1` (~20 tok/s, 96GB download), `laguna-s-2.1 (DFlash • vLLM)`, `laguna-s-2.1 (MAX • Modular)`, `qwen3-coder:30b` |

The Laguna entries are agentic-coding Mixture-of-Experts models from
Poolside, pinned to the `q4_K_M` tag so a scaffolded project gets a known
quantisation. `laguna-xs-2.1` (33B total, 3B active, 20GB) needs an AGX
Orin 32GB or larger; `laguna-s-2.1` (118B total, 8B active, 96GB) needs a
Thor 128GB class device. Neither fits a Pi 5 or an Orin Nano 8GB. Both
Ollama picks require an Ollama new enough to know the `laguna`
architecture, which is why the Ollama service here tracks the stock image
rather than a pinned JetPack build; the `laguna-s-2.1-dflash` pick replaces
Ollama with vLLM entirely (see "DFlash mode" below).

Two Thor-specific notes from on-device benchmarks (200 decoded tokens,
median of five runs). Decode speed tracks *active* parameters while prefill
tracks *total* ones, so XS decodes near a 3B dense model (~59 tok/s vs
~63 tok/s for `qwen2.5:3b`) despite 11x the total parameters — on Thor,
prefer XS unless you need S's extra capability, at a fifth of the download
and resident memory. And when capacity-planning either Laguna model, pin
`num_ctx`: left unpinned, Ollama sizes the KV cache from free memory, and
at 96GB of weights on a 128GB device that can tip into a partial CPU
offload that produces plausible-looking but degraded throughput.

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
  (Thor) kernels. The entrypoint preflights CUDA at startup and exits with
  a clear error — before the 72GB download — when the driver can't run this
  image, and also gives up (non-zero, restarted with backoff by compose)
  after repeated fast crashes, so a permanently broken platform surfaces as
  a restarting container instead of a healthy-looking crash loop.
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
- The API on :8000 requires the shared local API key (`wendy-local`) that
  Open WebUI is preconfigured with. It is a public template constant, not a
  secret — it stops casual unauthenticated use of the published port, same
  spirit as the Ollama port. Change `VLLM_API_KEY` (vllm service) and
  `OPENAI_API_KEY` (open-webui service) together.

## MAX mode (`laguna-s-2.1-max`)

[Modular MAX](https://docs.modular.com/max/) is a second way to serve Laguna:
its own graph compiler and kernels instead of vLLM's, behind the same
OpenAI-compatible API. This pick swaps the backend at scaffold time — the
rendered `docker-compose.yml`/`wendy.json` define a `max` service serving
`poolside/Laguna-S-2.1-NVFP4` instead of `ollama`, and Open WebUI points at it.
No speculative decoding here; it is a straight comparison point against the
Ollama and vLLM paths on the same weights.

Things to know:

- **The image is a pinned MAX nightly.** MAX added the Laguna architecture on
  2026-06-24, six days after 26.4.0 shipped, so no stable release can load this
  model yet. `Dockerfile` pins a dated `26.5.0.dev…` tag (multi-arch, so arm64
  Jetson/Spark hosts get an arm64 image); bump it to 26.5.0 once that releases.
- **Thor is a "known compatible for development" target for MAX, not a tested
  serving one** — only B200 is, and Modular verified Laguna itself on a B200.
  Expect to confirm throughput on your device rather than trusting a number
  from elsewhere. If NVFP4 Laguna will not load on your GPU, the fallbacks in
  order are: a newer nightly tag, then `MAX_TARGET_MODEL=poolside/Laguna-XS-2.1-NVFP4`
  (33B/3B, ~20GB, same code path). bfloat16 is the only other encoding MAX
  supports for Laguna and S 2.1 needs ~236GB of it, so it is not an option on a
  128GB device.
- **On Jetson, free the page cache before the first start.** Two MAX behaviours
  differ on Tegra, both handled automatically by `max/entrypoint.sh` when it
  sees `/dev/nvmap`, but one of them needs help from you:
  - MAX's device graph capture calls CUDA's virtual-memory-management API,
    which Tegra does not implement, so the model worker dies with
    `CUDA_ERROR_INVALID_DEVICE` in `vmmCreate`. The entrypoint passes
    `--no-device-graph-capture` on Tegra; discrete GPUs keep capture.
  - MAX reports GPU free memory as the host's `MemFree`, which does **not**
    count reclaimable page cache. On a device that has been serving models,
    almost all memory sits in page cache, so MAX sees a few GiB and refuses:
    `Model size exceeds available memory (66.98 GiB > 4.25 GiB)`. Nothing is
    actually wrong with the device — `MemAvailable` is the real number. Free
    the cache before starting (`sync && echo 3 > /proc/sys/vm/drop_caches` on
    the device host, or reboot) and the same model loads. The entrypoint warns
    when `MemFree` is under half of `MemAvailable`, and uses a 0.95 device
    memory fraction on Tegra (override with `TEGRA_DEVICE_MEMORY_UTILIZATION`)
    since 0.70 of an already-understated number rejects large models.
- Like the DFlash mode, the entrypoint preflights CUDA before the ~72GB
  download and exits non-zero (restarted with backoff by compose) on repeated
  fast crashes, so a platform mismatch surfaces as a restarting container
  instead of a healthy-looking crash loop.
- First start downloads ~72GB into the `…-hf` volume **and** compiles the model
  graph into the `…-maxcache` volume; the API stays down until both finish, so
  the UI comes up with an empty model list. Later starts reuse both — keep the
  volumes if you care about restart time.
- **The API on :8000 is unauthenticated.** MAX serve has no API-key option, so
  unlike the vLLM mode anything that can reach the device can use the model.
  The `OPENAI_API_KEY: "wendy-local"` on the open-webui service is a
  placeholder Open WebUI requires, not a credential.
- Tokens/second comes from MAX's Prometheus endpoint on :8001 — see
  "Useful commands" below.

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
> the local Compose URLs (`http://ollama:11434`, `http://vllm:8000`,
> `http://max:8000`) do not resolve from Open WebUI. The entrypoint rewrites
> whichever is configured to its `http://127.0.0.1:<port>` form on device
> because every backend publishes its API on the shared device network stack.
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
built-in service-name DNS. In DFlash or MAX mode the rendered compose file
needs an NVIDIA GPU host with ~80GB+ of free memory to be useful.

## Useful commands

```sh
wendy run --detach           # start and return; stream later with:
wendy device logs {{.APP_ID}} --service ollama --tail 100
wendy device apps list       # list both containers
```

In MAX mode, tokens/second after a generation — decode speed is the inverse of
time-per-output-token:

```sh
curl -s http://<device-hostname>:8001/metrics | grep time_per_output_token
# tok/s = 1000 / (…_sum / …_count)
```

