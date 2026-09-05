"""Keep the first-run surface small and the published artifacts self-contained."""

import hashlib
import importlib.util
import json
import pathlib
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_catalog_is_current():
    assert load_script("update_catalog").updated_readme() == (ROOT / "README.md").read_text()


def test_starters_are_target_appropriate():
    catalog = json.loads((ROOT / "meta.json").read_text())
    starters = [t for t in catalog["templates"] if t["category"] == "starter"]
    assert [t["name"] for t in starters if "wendyos" in t.get("targets", ["wendyos"])] == [
        "simple-api", "fullstack", "camera-feed", "audio"
    ]
    assert {t["name"] for t in starters if "wendy-lite" in t.get("targets", [])} == {"hello-world", "blink-led"}


def test_bundles_are_deterministic_and_contain_only_selected_project(tmp_path):
    builder = load_script("build_template_bundles")
    first = builder.build(tmp_path / "first")
    second = builder.build(tmp_path / "second")
    assert first == second
    for key, bundle in first["bundles"].items():
        path = tmp_path / "first" / bundle["path"]
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == bundle["sha256"]
        assert len(data) == bundle["size"]
        with tarfile.open(path) as archive:
            names = archive.getnames()
            assert f"templates/{key}/template.json" in names
            assert f"templates/{key}/wendy.json" in names
            assert all(name.startswith(f"templates/{key}/") for name in names)
            assert not any(part in {"node_modules", ".build", "target", ".git"} for name in names for part in pathlib.PurePosixPath(name).parts)


def test_web_app_is_small_and_needs_only_network(tmp_path):
    index = load_script("build_template_bundles").build(tmp_path)
    for key, bundle in index["bundles"].items():
        if not key.endswith("/fullstack"):
            continue
        root = ROOT / key
        config = json.loads((root / "wendy.json").read_text().replace("{{.APP_ID}}", "demo").replace("{{.PORT}}", "3001"))
        assert config["entitlements"] == [{"type": "network"}]
        with tarfile.open(tmp_path / bundle["path"]) as archive:
            generated = [n for n in archive.getnames() if not n.endswith(("template.json", "package-lock.json", "Cargo.lock", "Package.resolved"))]
        # Mojo vendors its small networking module because it has no stdlib HTTP server.
        assert len(generated) <= (18 if key.startswith("mojo/") else 15), (key, len(generated))
        assert bundle["size"] < 100_000, (key, bundle["size"])
