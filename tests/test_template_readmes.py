"""Catalog, placeholder, link, and shared-source consistency checks."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
META = json.loads((REPO_ROOT / "meta.json").read_text())
LANGUAGES = [entry["key"] for entry in META["languages"]]


def _repo_files() -> list[pathlib.Path]:
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    paths = [REPO_ROOT / name for name in listing.split("\0") if name]
    return [path for path in paths if path.is_file()]


# Git decides what is repository content, so build output such as
# node_modules/, .build/, and target/ never reaches these checks.
REPO_FILES = _repo_files()


def repo_files(directory: pathlib.Path) -> list[pathlib.Path]:
    return [path for path in REPO_FILES if path.is_relative_to(directory)]


def repo_markdown_files() -> list[pathlib.Path]:
    return [path for path in REPO_FILES if path.suffix == ".md"]


def catalog_projects() -> list[tuple[str, str, pathlib.Path]]:
    projects = []
    for template in META["templates"]:
        for language in template.get("languages", LANGUAGES):
            projects.append(
                (
                    template["name"],
                    language,
                    REPO_ROOT / language / template["name"],
                )
            )
    return projects


def catalog_languages(name: str) -> list[str]:
    for template in META["templates"]:
        if template["name"] == name:
            return template.get("languages", LANGUAGES)
    raise AssertionError(f"meta.json has no template named {name!r}")


def allowed_variables(project: pathlib.Path) -> set[str]:
    manifest = json.loads((project / "template.json").read_text())
    names = {"APP_ID"}
    names.update(variable["name"] for variable in manifest.get("variables", []))

    schema_path = project / "template.schema.json"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text())
        for phase in schema.get("phases", []):
            names.update(question["id"] for question in phase.get("questions", []))
    return names


def assert_common_files_match(
    source: pathlib.Path,
    consumer: pathlib.Path,
    ignored: set[pathlib.Path],
) -> None:
    for source_file in repo_files(source):
        relative = source_file.relative_to(source)
        if relative in ignored:
            continue
        consumer_file = consumer / relative
        assert consumer_file.exists(), f"{consumer_file} is missing shared file {relative}"
        assert consumer_file.read_bytes() == source_file.read_bytes(), (
            f"{consumer_file} drifted from {source_file}"
        )

    # Reverse pass: catch consumer copies left behind by a deletion in the
    # source tree. Limited to subdirectories the source also has, because a
    # consumer root can hold its own backend next to the shared frontend.
    for consumer_file in repo_files(consumer):
        relative = consumer_file.relative_to(consumer)
        if relative in ignored or relative.parent == pathlib.Path("."):
            continue
        if not (source / relative.parent).is_dir():
            continue
        assert (source / relative).exists(), (
            f"{consumer_file} has no counterpart in {source}; "
            f"delete the stale copy or restore the shared file"
        )


def test_catalog_projects_and_root_readmes_exist():
    projects = catalog_projects()
    assert projects, "meta.json lists no templates"

    for name, language, project in projects:
        assert project.is_dir(), f"catalog entry {language}/{name} has no directory"
        assert (project / "template.json").is_file()
        readme = project / "README.md"
        assert readme.is_file(), f"catalog entry {language}/{name} has no root README"


def test_markdown_template_variables_are_declared():
    action = re.compile(r"{{(.*?)}}", re.DOTALL)
    variable = re.compile(r"\.([A-Z][A-Z0-9_]*)")

    for _name, _language, project in catalog_projects():
        allowed = allowed_variables(project)
        for readme in repo_files(project):
            if readme.name != "README.md":
                continue
            used = {
                found
                for body in action.findall(readme.read_text())
                for found in variable.findall(body)
            }
            unknown = used - allowed
            assert not unknown, f"{readme} uses undeclared variables: {sorted(unknown)}"


def test_relative_markdown_links_resolve():
    link_pattern = re.compile(r"\[[^]]*]\(([^)]+)\)")

    for readme in repo_markdown_files():
        for target in link_pattern.findall(readme.read_text()):
            target = target.strip().strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_text = target.split("#", 1)[0]
            resolved = (readme.parent / path_text).resolve()
            assert resolved.exists(), f"{readme} has broken relative link {target!r}"


def test_shared_static_viewers_are_byte_identical():
    for family, common_dir in (
        ("audio", "audio-feed-html"),
        ("camera-feed", "camera-feed-html"),
    ):
        source = REPO_ROOT / "common" / common_dir / "index.html"
        for language in catalog_languages(family):
            consumer = REPO_ROOT / language / family / "index.html"
            assert consumer.is_file(), f"{consumer} is missing the shared viewer"
            assert consumer.read_bytes() == source.read_bytes(), (
                f"{consumer} drifted from {source}"
            )


def test_realsense_shared_frontend_drift():
    source = REPO_ROOT / "common" / "realsense-camera-frontend"
    assert_common_files_match(
        source,
        REPO_ROOT / "python" / "realsense-camera",
        {pathlib.Path("README.md")},
    )
    assert_common_files_match(
        source,
        REPO_ROOT / "cpp" / "realsense-camera",
        {pathlib.Path("README.md"), pathlib.Path("vite.config.ts")},
    )


def test_fullstack_shared_frontend_drift():
    source = REPO_ROOT / "common" / "shadcn-vite-frontend"
    ignored = {
        pathlib.Path("README.md"),
        pathlib.Path("src/pages/audio.tsx"),
        pathlib.Path("src/pages/camera.tsx"),
        pathlib.Path("src/lib/device-storage.ts"),
    }
    for language in catalog_languages("fullstack"):
        assert_common_files_match(
            source,
            REPO_ROOT / language / "fullstack" / "frontend",
            ignored,
        )
