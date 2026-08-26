# Fullstack frontend base

This React, Vite, Tailwind, and shadcn/ui project is the maintainer base for the
five `fullstack` frontends:

- `cpp/fullstack/frontend/`
- `node/fullstack/frontend/`
- `python/fullstack/frontend/`
- `rust/fullstack/frontend/`
- `swift/fullstack/frontend/`

The generated-project copies intentionally add `src/lib/device-storage.ts` and
adapt `src/pages/audio.tsx` and `src/pages/camera.tsx` to their backend behavior.
All other common files should remain synchronized. The repository test enforces
that boundary.

Run:

```sh
python3 -m pytest tests/test_template_readmes.py
```

For local development of the base UI:

```sh
npm install
npm run dev
```

Before merging frontend changes, run:

```sh
npm run lint
npm run build
```

The current ESLint config uses `tseslint.configs.recommended`, which does not
perform type-aware linting. Production projects can switch to a type-checked
typescript-eslint config and point its parser options at the existing TypeScript
project files.

The default Vite config has no backend proxy. Consumer pages call `/api/*` on
the current origin, so add a temporary proxy when developing against a separate
backend.

Add reusable pages, components, hooks, and styles here. Merge changes into all
five consumers while retaining their documented overrides. Add a component
with `npx shadcn@latest add <component>` and commit the generated source and
package changes.
