# {{.APP_ID}}

A Swift audio device demo for WendyOS. It streams microphone samples to a
browser waveform and plays bundled WAV files through a selected speaker.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- An ALSA-compatible microphone and speaker
- Network access during the first build to fetch Swift packages

The app declares host-network and audio entitlements. Microphone data is raw
signed 16-bit little-endian PCM, mono, at 16 kHz. Local development requires
Swift `{{.SWIFT_VERSION}}`, GStreamer, `arecord`, and `aplay`.

## Run and verify

Connect the audio devices before starting the app, then run:

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. Select a microphone and speak to see
the waveform. Select a speaker and play one of the bundled sounds.

The page uses `GET /microphones`, `GET /speakers`, `GET /sounds`,
`POST /speaker/<device>`, `POST /play/<file>`, and `WS /stream`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application, package, and executable name |
| `PORT` | `6004` | HTTP/WebSocket listener, readiness probe, and UI port |
| `SWIFT_VERSION` | `6.3` | Swift container toolchain tag |

## How it works

`Sources/{{.APP_ID}}/main.swift` discovers ALSA devices, owns the GStreamer process,
broadcasts PCM frames, handles sample playback, and defines the HTTP and
WebSocket routes. `StaticFileMiddleware.swift` serves `index.html` and assets.
WAV files live in `assets/`.

## Extend it

- Add a WAV file to `assets/`; it appears in the sound list automatically.
- Change the PCM caps in `main.swift` and the browser decoder in `index.html`
  together when changing format, channels, or sample rate.
- Add Hummingbird routes or controls and update `wendy.json` for new hardware.

Container deployment is the closest match to the WendyOS audio environment.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If a device is missing, check `arecord -l` and `aplay -l` on the target and
restart after connecting it. If a device is busy, stop the other process using
it before retrying.
