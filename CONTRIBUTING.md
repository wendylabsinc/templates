# Contributing templates

This guide is for repository maintainers. Generated-project instructions belong
in the README at each template root.

## Repository layout

Selectable templates use this layout:

```text
<language>/<template-name>/
├── README.md
├── template.json
├── wendy.json
└── project source
```

The public catalog is defined by `meta.json`. A template that is not listed
there is not selectable through the normal catalog.

Catalog entries declare `displayName`, `category` (`starter` or `example`), and
optional `requirements`. Keep identifiers stable for `--template` scripts.
WendyOS has four primary starters: API, Web app, Camera, and Audio. Larger
integrations belong under examples. Regenerate the README after catalog edits:

```sh
python3 scripts/update_catalog.py
```

The Web app (`fullstack`) has one React page, one greeting endpoint, and network
access. Keep generated projects within 15 files excluding lockfiles (18 for
Mojo's vendored networking). The former hardware dashboard lives in
`device-dashboard`; maintain its frontend in `common/shadcn-vite-frontend`.
The minimal Web app frontend lives in `common/react-frontend`.

## Template manifests

`template.json` declares the values collected by `wendy init`. The CLI makes
`APP_ID` available automatically, but a template may declare it to customize the
prompt.

```json
{
  "name": "simple-api",
  "description": "Minimal HTTP API",
  "variables": [
    {
      "name": "PORT",
      "description": "Primary HTTP port",
      "type": "integer",
      "default": 3001,
      "validate": { "min": 1, "max": 65535 }
    }
  ]
}
```

Supported variable types are `string`, `integer`, and `boolean`. String
validation may use `pattern`; integer validation may use `min` and `max`.

## Rendering

Files use Go `text/template` expressions:

```text
{{.APP_ID}}
{{.PORT}}
{{if .ENABLE_CORS}}...{{end}}
```

The CLI reads the manifest and schema, collects values, and writes every project
file except `template.json` and `template.schema.json`. Recognized text files
are rendered as Go templates; binary and unrecognized files are copied
unchanged. JSX and TSX are deliberately not rendered because their
object-expression syntax can collide with Go template actions. The CLI can also
initialize a Git repository. Keep conditionals small and provide usable
defaults.

The CLI writes the project to `./<app-id>/` and renames any directory named
after the template to the app ID. A Swift template's `Sources/<template-name>/`
becomes `Sources/<app-id>/` in the generated project. Use `{{.APP_ID}}` when a
README or a source file refers to such a path.

Test a branch through the CLI with:

```sh
wendy init --template <name> --language <language> --branch <branch>
```

## Shared sources

The `common/` directories are maintainer inputs, not generated projects. Read
the README in each common directory before copying changes. Some consumers have
documented overrides, so do not replace a consumer tree without reviewing its
differences.

## Tests

Run:

```sh
python3 -m pytest tests/
```

Build and smoke-test a generated Web app locally (Docker required):

```sh
python3 scripts/smoke_fullstack.py --language python
```

CI runs this against all six Web app languages on ARM64. It checks the health
response, greeting, HTML, and compiled JS/CSS responses. The command reports
render, build, startup, and image-size measurements separately; a cached image
startup is not a cold deployment measurement.

Tests check catalog and README coverage, template placeholders, shared-source
drift, and CUDA compatibility blocks. Add or update a test when a new shared
copy or catalog rule is introduced.

## Hosted template sources

Every push mirrors the repository to:

```text
https://templates.wendy.dev/<branch>/<path>
```

The deployment workflow is `.github/workflows/deploy-templates.yml`. It copies
the repository, except `.git`, `.github`, `tests`, `scripts`, and `.DS_Store` files, to the
branch prefix in the public bucket. Deleting a branch removes its prefix. The
CDN cache lifetime is five minutes.

The same workflow builds one deterministic archive per catalog language variant
with `scripts/build_template_bundles.py`. The branch's `template-index.json`
contains schema version `1`, the Git revision, the catalog, and a `bundles` map
keyed by `language/template`. Each entry has `path`, `sha256`, and compressed
`size`. Archives contain `templates/<language>/<template>/...`, so the CLI can
reuse its existing extraction and rendering code.

Publish immutable `bundles/<sha256>.tar.gz` objects before replacing the index.
Preserve old bundles while cached indexes can refer to them. New CLIs cache the
index for five minutes and verify bundle checksums before caching or rendering.
Older CLIs continue to use the repository archive and unchanged source paths.
The CLI also falls back to that archive for branches without an index.

Build the publisher output locally:

```sh
python3 scripts/build_template_bundles.py --output /tmp/template-bundles
```

Branch names containing slashes are preserved as URL path segments. Git does
not allow a branch and another branch with that branch as a path prefix, so the
deployment prefixes cannot overlap in that way.
