# {{.APP_ID}}

One React page and a small mojo backend. Requires a reachable WendyOS device,
Wendy CLI access, and network access for the first build. No special hardware.

```sh
wendy run
```

Your browser opens `http://<device-hostname>:{{.PORT}}` and displays
**Hello from Wendy!**. The first build downloads dependencies; later builds reuse caches.

## Make your first change

Change `Hello from Wendy!` in `main.mojo`, then run `wendy run` again.
Press **Refresh** to see your new message. Edit `frontend/src/main.tsx` for the
page and `frontend/src/style.css` for its appearance.

## How it works

- `GET /api/hello` returns `{"message":"Hello from Wendy!"}`.
- `GET /health` returns `{"status":"ok"}`.
- The backend serves the built frontend from `static/`.
- `Dockerfile` builds both parts. `wendy.json` grants network access, checks
  readiness, and opens the browser. `PORT` defaults to `3001`.

The **Device dashboard** example contains camera, audio, GPU, and persistent
SQLite demos. Add those capabilities when your application needs them.

## Local frontend development

With a backend running, start the frontend with `cd frontend`, `npm ci`, and
`npm run dev`. Set the `/api` proxy in `frontend/vite.config.ts` to your backend
URL (the default is `http://localhost:{{.PORT}}`). Run `npm run build` to check it.

## Troubleshooting

Run `wendy device logs {{.APP_ID}} --tail 100` if the page cannot connect.
Check that the device is reachable and port `{{.PORT}}` is free.
Stop the app with `wendy device apps stop {{.APP_ID}}`.
