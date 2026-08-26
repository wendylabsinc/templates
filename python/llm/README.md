# {{.APP_ID}}

A local chat application for WendyOS. It runs Ollama and Open WebUI as a
two-service app group and keeps model weights and WebUI data in persistent
volumes.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- Internet access on the first run to pull container dependencies and the
  selected Ollama model
- Free disk space larger than the model download plus Open WebUI data
- Enough system or GPU memory for the selected model

Start with the default small model unless you already know the target's
available memory. Model download and loading can make the first start much
slower than later starts.

## Run and verify

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. The Ollama service pulls
`{{.OLLAMA_MODEL}}` in the background. The model appears in Open WebUI after the
pull finishes; start a new chat and send a short prompt.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | App-group and persistent-volume prefix |
| `PORT` | `8080` | Public Open WebUI port and readiness probe |
| `OLLAMA_MODEL` | `gemma4:e2b` | Model pulled by Ollama on startup |

To switch models after generation, change `OLLAMA_MODEL` in
`docker-compose.yml` and run the app again. Useful small alternatives include
`qwen2.5:3b`, `llama3.2:3b`, and `gemma3:4b`; check the model size against the
target's free memory and disk before selecting a larger model.

## How it works

`docker-compose.yml` defines the `ollama` and `open-webui` services, their
ports, dependency, environment, and named volumes. The companion `wendy.json`
adds the app ID, Ollama GPU entitlement, readiness probe, and browser hook.
`wendy run` merges the two files.

| Service | Port | Persistent data |
|---|---:|---|
| `ollama` | `11434` | Model files in the `{{.APP_ID}}-models` volume |
| `open-webui` | `{{.PORT}}` | Accounts, chats, and settings in the `{{.APP_ID}}-openwebui` volume |

On WendyOS, the Open WebUI entrypoint uses `127.0.0.1:11434` to reach Ollama on
the shared device network. With local Docker Compose, it uses
`http://ollama:11434` through Compose DNS.

## Extend it

- Change model setup and retry behavior in `ollama/entrypoint.sh`.
- Change Open WebUI settings or branding in `open-webui/`.
- Add a service to `docker-compose.yml` and add Wendy-specific entitlements for
  it under the matching service key in `wendy.json`.

For local Docker development:

```sh
docker compose up --build
```

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --service ollama --tail 150
wendy device logs {{.APP_ID}} --service open-webui --tail 100
wendy device apps stop {{.APP_ID}}
```

An empty model picker usually means the model is still downloading or failed to
pull. Check Ollama logs and device network access. Removing the app does not
delete its model or WebUI volumes unless you explicitly request volume deletion.
