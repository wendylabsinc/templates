# wendynet (spike seed)

Pure-Mojo HTTP/1.1 + WebSocket (RFC 6455) serving layer for the Mojo template ports —
Mojo 1.0's stdlib has no networking, JSON, or SHA-1 (see `docs/mojo-max-port-findings.md`,
MMF-011), and the community `lightbug_http` was archived in May 2026, so this is hand-rolled
on libc socket FFI.

Current state: **Spike 1 output** — a single-file echo server proving the full stack on
Mojo 1.0 / linux-arm64 (MAX 26.5 image). It will be refactored into a proper package
(`tcp` / `http` / `ws` / `json` modules, concurrent connections) as `mojo/simple-api` lands.

- `ws_echo.mojo` — TCP listener (libc FFI), minimal HTTP parsing, RFC 6455 handshake
  (hand-rolled SHA-1, self-tested against the RFC sample key), frame codec (7/16/64-bit
  lengths, masking), text/binary echo, ping→pong, clean close. Single connection at a time.
- `test_ws_echo.py` — dependency-free raw-socket test suite (verifies the
  `Sec-WebSocket-Accept` digest independently, echo integrity, extended lengths, ping, close).

Run it:

```sh
mojo build -o ws_echo ws_echo.mojo && ./ws_echo   # listens on :9099
python3 test_ws_echo.py                            # in another shell
```

Verified 2026-08-23 in the `python-starter-max` (MAX 26.5.0 / Mojo 1.0.0) arm64 container:
all 6 tests pass.
