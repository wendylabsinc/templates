# Shared camera viewer

`index.html` is the maintainer source for the static camera UI copied into every
language implementation of the `camera-feed` template.

Consumers:

- `cpp/camera-feed/index.html`
- `node/camera-feed/index.html`
- `python/camera-feed/index.html`
- `rust/camera-feed/index.html`
- `swift/camera-feed/index.html`

Keep those files byte-identical. Edit this file first, copy it to every
consumer, and run:

```sh
python3 -m pytest tests/test_template_readmes.py
```

For a layout-only preview, run `python3 -m http.server` in this directory. The
camera list and image require a running template backend.

When changing the browser protocol, update every backend before copying the UI.
The current contract uses `GET /cameras`, the `/stream` WebSocket, binary MJPEG
frames, and JSON `switch_camera` messages.
