# {{.APP_ID}}

A C++ audio device demo for WendyOS. It streams microphone samples to a browser
waveform and plays bundled WAV files through a selected speaker.

## Requirements

- A reachable ARM64 WendyOS device and Wendy CLI access
- An ALSA-compatible microphone and speaker
- Network access during the first build to fetch Drogon

The app declares host-network and audio entitlements. Microphone data is raw
signed 16-bit little-endian PCM, mono, at 16 kHz.

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
| `APP_ID` | required | Application and executable name |
| `PORT` | `7004` | HTTP/WebSocket listener, readiness probe, and UI port |

## How it works

`main.cpp` discovers ALSA devices, starts and manages the GStreamer capture
pipeline for the service lifetime, broadcasts PCM frames to connected clients,
and starts a separate GStreamer pipeline for sample playback.
`index.html` renders the waveform and remembers device selections in the
browser. WAV files live in `assets/`.

## Extend it

- Add a WAV file to `assets/`; it appears in the sound list automatically.
- Change the PCM caps in `main.cpp` and the browser decoder in `index.html`
  together when changing format, channels, or sample rate.
- Add Drogon routes or controls and update `wendy.json` for any new hardware.

For local development, install Drogon, GStreamer, ALSA development packages,
`arecord`, and `aplay`, then build with CMake.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If a device is missing, check `arecord -l` and `aplay -l` on the target and
restart after connecting it. If a device is busy, stop the other process using
it before retrying.
