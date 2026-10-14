# Tests

Run the repository checks from the repository root:

```sh
python3 -m pytest tests/
```

CI runs the same command on every pull request.

## Coverage

- `test_cuda_compat_parity.py` checks that the JetPack CUDA compatibility block
  is present in the expected Dockerfiles and remains byte-identical.
- `test_template_readmes.py` checks catalog directories, project README
  coverage, supported README placeholders, relative Markdown links, and
  shared-source drift in both directions.
- `test_go2_sim.py` checks the `go2-sim` template variables, its `wendy.json`
  and `Dockerfile` changes, and renders the template the way `wendy init` does
  to prove every other file, including the vendored Three.js modules, comes out
  byte for byte unchanged.

The checks read Git-tracked files and non-ignored untracked files, so ignored
build output such as `node_modules/`, `.build/`, and `target/` is never scanned.

Add a test when introducing a selectable template, a shared source copy, or a
repository rule that would otherwise drift silently.
