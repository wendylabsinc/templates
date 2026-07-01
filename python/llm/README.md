# llm

Local LLM chat app built as a **multi-service app group**: an Ollama model
server and an Open WebUI frontend, each in its own container, defined by a
standard `docker-compose.yml` with a companion `wendy.json`.

```
llm/
├── docker-compose.yml   ← service topology (fully Docker Desktop-compatible)
├── wendy.json           ← companion: appId + GPU entitlement for ollama
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
  }
}
```

When you `wendy run`, the CLI merges both files:

- Topology, `ports`, `environment`, named volumes, and `depends_on` come from
  `docker-compose.yml`. Port mappings become `network` entitlements; named
  volumes become `persist` entitlements automatically.
- `appId` and the per-service `gpu` entitlement come from `wendy.json`.

## Services

| Service | Port | Purpose |
|---------|------|---------|
| `ollama` | 11434 | Ollama API. Pulls the configured model in the background on first start; weights persist in the `…-models` volume. |
| `open-webui` | {{.PORT}} | Chat UI. Persists user data in the `…-openwebui` volume. |

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

> Compose app groups do not yet support Wendy readiness probes or
> `postStart` hooks, so the browser is not opened automatically.

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
