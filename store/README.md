# Template apps in Wendy Companion

The templates remain editable source projects for `wendy init`. This directory
adds a curated release catalog for the existing App Market in Wendy Companion.
There is one app per user-facing function, rather than one per language.

Companion reads `https://templates.wendy.dev/main/store/catalog.json` alongside
the existing App Market catalog. App IDs use the `template-` prefix. A listing
without a qualified release shows Coming Soon and cannot resolve an install.

## Current scope

`meta.json` supplies a listing for every template, with one canonical language
per app. `apps.json` curates five starting listings. SO-101 Simulator and Simple API have
fixed, single-service package recipes. Both target Linux ARM64 and AMD64. The
simulator is virtual only and never opens a physical robot's servos. Camera,
Audio, and Dashboard are preview listings pending their runtime contracts.
No image releases or device qualifications are included in this change.

Native Mac applications and Wendy Lite firmware require their own artifact
and installation paths. Multi-service apps such as G1 RC, Go2 RC, and local LLM
need group lifecycle, dependencies, durable settings and secrets, and readiness
for every service before becoming packageable here. They must not be flattened
to a single container. Hardware apps also need peripheral and resource-conflict
checks; an architecture match alone is insufficient.

## Build a candidate

Run the manual **Build App Market candidate** GitHub workflow for `so101-sim`
or `simple-api` at a reviewed source revision. It renders all template defaults,
builds both Linux architectures, pushes a candidate image to GHCR, and saves
`release-candidate.json` and the exact `wendy.json` as a workflow artifact.
Candidate construction does not publish an installable catalog release.

The GHCR package must permit anonymous pulls before publishing a release. Do not
put registry credentials in the catalog. First-time package creation can require
repository access to its organization package.

Local preparation and checks:

```sh
python3 -m unittest discover -s tests -p test_app_market.py
python3 tools/app_market.py render so101-sim /tmp/so101-app-candidate
python3 tools/app_market.py catalog --output store/catalog.json
```

The render destination must be new. It contains the existing template Dockerfile
and source with fixed defaults. Its Wendy configuration retains entitlements and
readiness, publishes the UI's HTTP port, and removes the developer's browser
opening hook. Installation sends that exact configuration to the agent.

## Qualify and promote

For each of `linux/amd64` and `linux/arm64`, use a supported WendyOS device to
install the candidate by immutable digest with its exact configuration. Record
the agent version, device identity and architecture, image digest, configuration
hash, install result, start result, expected HTTP response, opening the UI from
Companion, and persistence/restart behavior where applicable. Test port conflicts
and failure presentation. A started-container acknowledgement is not a health
check. Downloads and first launch still take time; there is no zero-second
installation promise.

SO-101 Simulator must serve its viewer and `/api/status`; Simple API must return
`{"status":"ok"}` at `/health`. Confirm these via the device's actual endpoint.
Keep evidence free of credentials and put a durable HTTPS evidence link in each
platform's qualification record. This is a maintainer declaration backed by that
evidence, not cryptographic remote attestation.

Add the candidate under its app ID in `releases.json`, including:

```json
{
  "releases": {
    "template-simple-api": {
      "image_reference": "ghcr.io/wendylabsinc/template-simple-api@sha256:<64 lowercase hex characters>",
      "source_revision": "<40 lowercase hex characters>",
      "platforms": ["linux/amd64", "linux/arm64"],
      "app_config_json": "<exact candidate wendy.json as a JSON string>",
      "qualification": {
        "linux/amd64": {
          "image_reference": "<same immutable reference>",
          "app_config_sha256": "<SHA-256 of the exact app_config_json UTF-8 bytes>",
          "evidence_url": "https://<durable-evidence-location>",
          "install_passed": true,
          "health_passed": true
        },
        "linux/arm64": {
          "image_reference": "<same immutable reference>",
          "app_config_sha256": "<same configuration hash>",
          "evidence_url": "https://<durable-evidence-location>",
          "install_passed": true,
          "health_passed": true
        }
      }
    }
  }
}
```

Review and merge that change. The existing templates publishing workflow validates
the release records, generates `store/catalog.json`, and uploads it with the
repository tree. A release change invalidates the previous image/configuration
qualification. Removing the release returns the listing to Coming Soon; it does
not remove an app already installed on a user's device.

Companion checks the connected device's reported OS and CPU architecture and
whether its transport supports installation before enabling selection, then
checks again at install. Unknown or unreachable devices stay unavailable. The
current BLE and Cloud v2 transports do not implement image installation; local
gRPC and the existing legacy Cloud tunnel can use it. Settings, models, secrets,
rollback, upgrades, health-gated completion, and Cloud v2 install support remain
separate work before expanding this initial package set.
