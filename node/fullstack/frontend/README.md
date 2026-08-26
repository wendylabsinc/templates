# Frontend development

This React frontend is built into the Node fullstack application. The production
Dockerfile runs `npm run build`; Express serves the compiled files and `/api/*`
from the same origin.

```sh
npm install
npm run dev
```

Before committing frontend changes, run:

```sh
npm run lint
npm run build
```

The current ESLint config uses `tseslint.configs.recommended`, which does not
perform type-aware linting. For stricter production checks, use a type-checked
typescript-eslint config and point its parser options at the existing TypeScript
project files.

The development server can display the UI shell, but `vite.config.ts` has no API
proxy. Add a local proxy for `/api` when working against a separately running
backend on port `{{.PORT}}`.

Pages live in `src/pages/`, navigation and routes in `src/App.tsx`, shared API
and storage helpers in `src/lib/`, and reusable controls in `src/components/`.
To add a feature, create the backend route first, add the page and API call, then
register its route and navigation entry.

This directory is based on `common/shadcn-vite-frontend`, with intentional
camera/audio device adaptations. Do not replace it wholesale with the common
tree.
