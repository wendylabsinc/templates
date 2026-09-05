# simple-api (Mojo)

The minimal REST API template in pure Mojo — same endpoints as the other language variants
(`GET /`, `GET /health`, `POST /items`), served by `wendynet`, the hand-rolled HTTP layer
vendored from `common/mojo/wendynet` (Mojo 1.0 has no stdlib networking or JSON; see
`docs/mojo-max-port-findings.md`, MMF-011).

The final image is `debian:bookworm-slim` + a ~50 KB binary + ~2 MB of Mojo runtime
libraries — roughly 90 MB total, no Python inside.

```sh
wendy init --template simple-api --language mojo my-api
cd my-api
wendy run
```
