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
completes.

> On the device each service runs in its own network namespace, so the
> Docker service-name URL (`http://ollama:11434`) does not resolve. The
> `open-webui` entrypoint detects the agent-injected `WENDY_DEVICE_HOSTNAME`
> and rewrites `OLLAMA_BASE_URL` to reach Ollama's host-published port
> instead.

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
wendy device logs
wendy device ps              # list both containers
```
