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

Tests check catalog and README coverage, template placeholders, shared-source
drift, and CUDA compatibility blocks. Add or update a test when a new shared
copy or catalog rule is introduced.

## Hosted template sources

Every push mirrors the repository to:

```text
https://templates.wendy.dev/<branch>/<path>
```

The deployment workflow is `.github/workflows/deploy-templates.yml`. It copies
the repository, except `.git`, `.github`, `tests`, and `.DS_Store` files, to the
branch prefix in the public bucket. Deleting a branch removes its prefix. The
CDN cache lifetime is five minutes.

Branch names containing slashes are preserved as URL path segments. Git does
not allow a branch and another branch with that branch as a path prefix, so the
deployment prefixes cannot overlap in that way.
