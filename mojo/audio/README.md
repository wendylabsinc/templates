# audio (Mojo)

Live microphone streaming and wav playback on WendyOS in pure Mojo:
`wendyaudio` talks to ALSA through `libasound.so.2` loaded at runtime with
`OwnedDLHandle` (no GStreamer, no link-time audio dependency), and `wendynet`
fans S16LE mono 16 kHz PCM out to browsers over hand-rolled WebSockets. Same
endpoints and client protocol as the `python/audio` (GStreamer) sibling:
`/` waveform UI, `/microphones`, `/speakers`, `/sounds`, `POST /play/{wav}`,
`POST /speaker/{id}`, `/logs`, `/debug`, and the `/stream` WebSocket with
`{"switch_microphone": "<id>"}` commands (acked with `mic_switched` /
`mic_switch_failed`).

Everything runs on one `poll(2)` loop: ALSA devices open non-blocking, the
mic is drained and broadcast each 50 ms tick, and wav playback feeds the
speaker as it accepts frames — no threads anywhere.

## Notes

- Devices are enumerated straight from `/proc/asound` (no `arecord`/`aplay`
  subprocess); ids are the usual `hw:CARD,DEV`, opened through ALSA's plug
  layer so format/rate conversion works on any hardware.
- `snd_pcm_set_params()` does all format negotiation — no hw_params struct
  ABI anywhere (the whole libasound surface used is ~10 functions).
- Playback expects 16-bit PCM wav files (the shipped sounds; the parser
  walks RIFF chunks and rejects anything else cleanly).
- ~110 MB final image: AOT binary + Mojo runtime `.so` set (MMF-009) +
  `libasound2`. No Python, no SDK at runtime.

## Verified

Jetson Orin Nano (WendyOS 0.18.2, JetPack 7.2), Logitech Brio 101 mic —
real-time 16 kHz capture (32k samples in 2.008 s) with live room-noise
amplitudes; see Appendix B of `docs/mojo-max-port-findings.md`.
