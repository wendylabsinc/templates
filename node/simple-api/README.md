# {{.APP_ID}}

A small TypeScript HTTP API built with Express. It provides a greeting, a
health check, and an example JSON endpoint for creating an item.

## Requirements

- A reachable WendyOS device
- Wendy CLI access to that device
- Network access during the first build to install npm packages

No camera, audio device, or GPU is required. Local development requires Node.js
22 or later.

## Run

```sh
wendy run
```

The post-start hook opens `http://<device-hostname>:{{.PORT}}`. You can also
check the API directly:

```sh
curl http://<device-hostname>:{{.PORT}}/health
curl -X POST http://<device-hostname>:{{.PORT}}/items \
  -H 'content-type: application/json' \
  -d '{"name":"sensor","price":12.50}'
```

The health response is `{"status":"ok"}`. The item endpoint returns the
submitted name and price with example ID `1`; it does not store the item.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application and npm package name |
| `PORT` | `5001` | HTTP listener, readiness probe, and browser port |

`wendy.json` grants network access, waits for the TCP port for up to 30 seconds,
and opens the root URL after startup.

## How it works

- `src/index.ts` defines `GET /`, `GET /health`, and `POST /items`.
- `package.json` provides TypeScript build, start, and watch scripts.
- `Dockerfile` compiles the source and runs `dist/index.js` with Node.js 22.

## Extend it

Add Express routes and middleware in `src/index.ts`. Replace the fixed item
response with a database or another service, and add any required entitlement
or persistent volume to `wendy.json`.

For local development:

```sh
npm install
npm run dev
```

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If readiness times out, check that port `{{.PORT}}` is free and inspect the
first build or application error in the logs.
