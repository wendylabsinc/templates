# Shared Web app frontend

One React page calls `GET /api/hello` and displays its `message` string.
The six `fullstack/frontend/` copies must match this directory, except this README.
Edit here, copy to all consumers, and run `python3 -m pytest tests/`.

Render `PORT` in `vite.config.ts` before local development. Then run `npm ci`
and `npm run dev`; `/api` is proxied to the backend. Run `npm run build` before
copying a change. Dependencies are locked in `package-lock.json`.
