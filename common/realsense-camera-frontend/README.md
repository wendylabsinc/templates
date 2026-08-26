# Shared RealSense frontend

This directory is the maintainer source for the React frontend used by:

- `python/realsense-camera/`
- `cpp/realsense-camera/`

The consumers include the frontend at their template root because `wendy init`
extracts only one language/template directory. Keep the common files and both
copies synchronized, with these exceptions:

- Generated-project READMEs are owned by each template.
- `cpp/realsense-camera/vite.config.ts` uses the C++ backend port; the common and
  Python copies use the Python port.

Run the drift check after every shared frontend change:

```sh
python3 -m pytest tests/test_template_readmes.py
```

For local UI development:

```sh
npm install
npm run dev
```

Vite proxies `/stream`, `/start`, `/stop`, `/config`, and `/health` to the
configured backend. Update the backend API and both generated-project READMEs
when changing that contract.
