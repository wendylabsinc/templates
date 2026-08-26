# Shared audio viewer

`index.html` is the maintainer source for the static audio UI copied into every
language implementation of the `audio` template.

Consumers:

- `cpp/audio/index.html`
- `node/audio/index.html`
- `python/audio/index.html`
- `rust/audio/index.html`
- `swift/audio/index.html`

Keep those files byte-identical. Edit this file first, copy it to every
consumer, and run:

```sh
python3 -m pytest tests/test_template_readmes.py
```

For a layout-only preview, run `python3 -m http.server` in this directory. Audio
device lists, playback, and the WebSocket waveform require a running template
backend.

When changing the browser protocol, update every backend before copying the UI.
The current contract uses `GET /microphones`, `GET /speakers`, `GET /sounds`,
`POST /speaker/<device>`, `POST /play/<file>`, and the `/stream` WebSocket with
16 kHz mono PCM16 frames.
