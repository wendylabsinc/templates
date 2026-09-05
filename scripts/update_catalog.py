"""Generate README catalog rows from meta.json; use --check in CI."""

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
START = "<!-- catalog:start -->"
END = "<!-- catalog:end -->"


def render():
    meta = json.loads((ROOT / "meta.json").read_text())
    languages = {item["key"]: item["name"] for item in meta["languages"]}
    lines = [START]
    for category, heading in [("starter", "Starters"), ("example", "Examples")]:
        lines.extend(["", f"### {heading}", "", "| Project | Languages | What you get |", "|---|---|---|"])
        for item in meta["templates"]:
            if item["category"] != category:
                continue
            name = item["name"]
            links = ", ".join(f"[{languages[lang]}]({lang}/{name}/)" for lang in item["languages"])
            target = ", ".join(item.get("targets", ["wendyos"]))
            description = item["description"] + f". Target: {target}."
            if item.get("requirements"):
                description += " Requires: " + item["requirements"] + "."
            lines.append(f"| {item['displayName']} (`{name}`) | {links} | {description} |")
    return "\n".join(lines + ["", END])


def updated_readme():
    readme = (ROOT / "README.md").read_text()
    start = readme.index(START)
    end = readme.index(END) + len(END)
    return readme[:start] + render() + readme[end:]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = updated_readme()
    if args.check:
        if result != (ROOT / "README.md").read_text():
            raise SystemExit("Catalog is stale; run python3 scripts/update_catalog.py")
    else:
        (ROOT / "README.md").write_text(result)
