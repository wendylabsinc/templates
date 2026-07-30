# llm

Local LLM chat app built as a **multi-service app group**: an Ollama model
server and an Open WebUI frontend, each in its own container, defined by a
standard `docker-compose.yml` with a companion `wendy.json`.

```
llm/
├── docker-compose.yml   ← service topology (fully Docker Desktop-compatible)
├── wendy.json           ← companion: appId, GPU entitlement, readiness + postStart hook
├── ollama/              ← Ollama server; pulls the chosen model on first start
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

## Choosing a model

The model is picked when the template is scaffolded (the `OLLAMA_MODEL`
variable; the full curated picker lives in `template.schema.json`). The
default is `gemma4:e2b`. Rough guidance by device:

| Device | Good picks |
|--------|------------|
| Raspberry Pi 5 | `gemma4:e2b` (slow), `qwen2.5:3b`, `llama3.2:3b` |
| Jetson Orin Nano 8GB | `gemma4:e2b`/`e4b`, `qwen2.5:3b` (~30 tok/s), `llama3.2:3b`, `gemma3:4b`, `mistral:7b` (~15 tok/s), `nemotron-3-nano-4b` |
| Jetson AGX Orin 32/64GB | `gemma4:26b`, `gemma4:31b` (64GB), `nemotron-3-nano:30b`, `qwen3-coder:30b` |
| Jetson AGX Thor (128GB) | `gpt-oss:120b`, `nemotron-3-super:120b`, `qwen3-coder:30b`, `laguna-s-2.1` (96GB download) |

The two Laguna entries are agentic-coding Mixture-of-Experts models. `laguna-xs-2.1`
(33B total, 3B active, 20GB) needs an AGX Orin 32GB or larger; `laguna-s-2.1`
(118B total, 8B active, 96GB) needs a Thor 128GB class device. Neither fits a
Pi 5 or an Orin Nano 8GB. Both require an Ollama new enough to know the `laguna`
architecture, which is why the Ollama service here tracks the stock image rather
than a pinned JetPack build.

To switch models later, edit the `OLLAMA_MODEL` environment value in
`docker-compose.yml` and re-run; the entrypoint pulls whatever it is set to.

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
> the local Compose URL (`http://ollama:11434`) does not resolve from Open
> WebUI. The entrypoint rewrites that URL to `http://127.0.0.1:11434` on
> device because Ollama publishes its API on the shared device network stack.
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
built-in service-name DNS.

## Useful commands

```sh
wendy run --detach           # start and return; stream later with:
wendy device logs {{.APP_ID}} --service ollama --tail 100
wendy device apps list       # list both containers
```
