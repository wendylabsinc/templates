#!/usr/bin/env python3
"""Package curated templates and generate Companion's release-backed catalog.

Only explicitly qualified, digest-pinned releases get an Install button. Build
outputs alone are candidates; they are never treated as device verification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
VARIABLE = re.compile(r"{{\s*\.([A-Z_][A-Z_0-9]*)\s*}}")
DIGEST = re.compile(r"ghcr\.io/wendylabsinc/template-[a-z0-9-]+@sha256:[a-f0-9]{64}")


def read(path):
    return json.loads(path.read_text())


def definitions(root=ROOT):
    curated = read(root / "store/apps.json")["apps"]
    known = {a["template"] for a in curated}
    for template in read(root / "meta.json")["templates"]:
        if template["name"] in known:
            continue
        languages = template.get("languages", ["python"])
        language = "python" if "python" in languages else languages[0]
        name = template["name"]
        curated.append({
            "template": name, "language": language,
            "name": name.replace("-", " ").title(),
            "category": "Robotics" if name.startswith(("g1-", "go2-", "so101-", "elrobot-", "rc-")) else "Developer Tools",
            "subcategory": "Templates", "package": False,
        })
    return curated


def render(root, app, destination):
    if not app["package"]:
        raise ValueError("This app still needs a hardware/configuration install contract")
    source = root / app["language"] / app["template"]
    if destination.exists():
        raise ValueError("Build destination must not already exist")
    schema = read(source / "template.json")
    variables = {v["name"]: v["default"] for v in schema["variables"] if "default" in v}
    variables["APP_ID"] = "template-" + app["template"]
    for v in schema["variables"]:
        if v.get("required") and v["name"] not in variables:
            raise ValueError(f"Missing required variable {v['name']}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".venv", ".git"))
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        text = VARIABLE.sub(lambda m: str(variables[m[1]]), text)
        if "{{" in text and path.name in ("Dockerfile", "wendy.json", "Stagefile"):
            raise ValueError(f"Unresolved build template in {path.name}")
        path.write_text(text)
    config = read(destination / "wendy.json")
    # Browser opening is a client action, not an on-device install hook.
    config.pop("hooks", None)
    # Publish the UI port for Companion's existing app-opening flow.
    port = variables["PORT"]
    config["entitlements"] = [e for e in config["entitlements"] if e["type"] != "network"]
    config["entitlements"].extend([
        {"type": "network", "mode": "host"},
        {"type": "http", "port": port},
    ])
    (destination / "wendy.json").write_text(json.dumps(config, indent=2) + "\n")
    return config


def catalog(root=ROOT):
    metadata = {a["name"]: a for a in read(root / "meta.json")["templates"]}
    releases = read(root / "store/releases.json")["releases"]
    apps = []
    for app in definitions(root):
        slug = "template-" + app["template"]
        source = root / app["language"] / app["template"]
        info = metadata[app["template"]]
        description = info["description"]
        release = releases.get(slug)
        item = dict(slug=slug, name=app["name"], tagline=description,
                    description=description, category=app["category"], subcategory=app["subcategory"],
                    icon_path="", screenshot_paths=[], entitlements=[], featured=False,
                    coming_soon=release is None, maintainer="Wendy Labs",
                    source_url=f"https://github.com/wendylabsinc/templates/tree/main/{app['language']}/{app['template']}")
        if release is not None:
            if (not app["package"] or not DIGEST.fullmatch(release["image_reference"])
                    or not release["image_reference"].startswith(f"ghcr.io/wendylabsinc/{slug}@sha256:")):
                raise ValueError(f"Invalid image release for {slug}")
            if not re.fullmatch(r"[a-f0-9]{40}", release["source_revision"]):
                raise ValueError(f"A source commit is required for {slug}")
            if release["platforms"] != ["linux/amd64", "linux/arm64"]:
                raise ValueError(f"Both Linux architectures must be qualified for {slug}")
            for platform in release["platforms"]:
                evidence = release["qualification"][platform]
                if (evidence.get("image_reference") != release["image_reference"]
                        or evidence.get("app_config_sha256") != hashlib.sha256(release["app_config_json"].encode()).hexdigest()
                        or evidence.get("install_passed") is not True or evidence.get("health_passed") is not True
                        or not evidence.get("evidence_url", "").startswith("https://")):
                    raise ValueError(f"Missing image-bound device qualification for {slug}: {platform}")
            config = json.loads(release["app_config_json"])
            if config["appId"] != slug or config["platform"] != "linux" or "services" in config:
                raise ValueError(f"Invalid single-service configuration for {slug}")
            if config.get("hooks") or "{{" in release["app_config_json"]:
                raise ValueError(f"Unrendered/client hooks in {slug}")
            ports = [e.get("port") for e in config["entitlements"] if e["type"] == "http"]
            if len(ports) != 1 or type(ports[0]) is not int or not 1 <= ports[0] <= 65535:
                raise ValueError(f"One valid HTTP port is required for {slug}")
            item["template_release"] = {k: release[k] for k in ("image_reference", "app_config_json", "platforms")}
            item["entitlements"] = sorted({e["type"] for e in config["entitlements"]})
        apps.append(item)
    unknown = set(releases) - {a["slug"] for a in apps}
    if unknown:
        raise ValueError(f"Unknown releases: {sorted(unknown)}")
    return {"schema_version": 1, "apps": apps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("render")
    build.add_argument("app", choices=[a["template"] for a in definitions()])
    build.add_argument("destination", type=Path)
    output = sub.add_parser("catalog")
    output.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "render":
        render(ROOT, next(a for a in definitions() if a["template"] == args.app), args.destination)
    else:
        result = json.dumps(catalog(), indent=2) + "\n"
        if args.output:
            args.output.write_text(result)
        else:
            print(result, end="")


if __name__ == "__main__":
    main()
