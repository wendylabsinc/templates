# Instant starters: rollout and validation

This change makes `fullstack` a small Web app in all six languages, preserves the
previous application as `device-dashboard`, and separates the catalog into
starters and examples. Existing generated projects are not modified.

The publisher produces one content-addressed archive per catalog variant.
Deploy the templates change before releasing the companion CLI change. The CLI
adds grouped selection, silent defaults, a remembered language, default Git
initialization, bundle caching, and `wendy init ... --run`. Existing explicit
template names, `--var`, `--language`, `--git-init`, and branch previews remain
supported. Older CLIs can read the new catalog and source trees.

## Measurements

The previous Web app variants contained 67–90 tracked files, including template
metadata and lockfiles. They combined a dashboard, persistent SQLite data,
camera/audio streams, and GPU/device pages. The new versions serve one React
page and expose `GET /api/hello` plus `GET /health`, with only network access.

Use `python3 scripts/build_template_bundles.py --output /tmp/template-bundles`
to measure transfer sizes from `template-index.json`. Measure actual runtime
behavior with `python3 scripts/smoke_fullstack.py --language <language>`. Its JSON
result separates rendering, container building, startup, and image bytes.

Keep cold and cached measurements separate. Record the host, device, architecture,
network, CLI revision, and which dependency/image caches were warm. The proposed
targets remain cached scaffolding below 2 seconds and a basic cached deployment
below 15 seconds on an agreed reference device; this PR does not claim those
device deployment targets have been measured. Container startup alone does not
measure transfer to a device or browser readiness.

## Follow-up work

- Measure novice completion: can a person create an app and make a visible edit
  within two minutes on a prepared device?
- Publish and validate supported runtime images for Camera and Audio, including
  architecture and JetPack compatibility. Change their base images only once
  those artifacts exist and have passed hardware tests.
- Measure camera/audio startup with physical devices before extracting more
  device code or removing media dependencies.
- Infer an ambiguous target from a connected device after validating WendyOS,
  Wendy Lite, and native Mac discovery paths. Explicit template targets already
  bypass the target prompt.

These hardware-dependent changes are separate from the small Web app and CLI
delivery changes, which can be exercised deterministically in CI.
