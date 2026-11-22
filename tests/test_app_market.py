import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("app_market", ROOT / "tools/app_market.py")
market = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market)


class AppMarketTests(unittest.TestCase):
    def test_unreleased_apps_cannot_offer_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store").mkdir()
            (root / "meta.json").write_bytes((ROOT / "meta.json").read_bytes())
            (root / "store/apps.json").write_bytes((ROOT / "store/apps.json").read_bytes())
            (root / "store/releases.json").write_text('{"releases":{}}')
            catalog = market.catalog(root)
            self.assertEqual(len(catalog["apps"]), len(market.read(ROOT / "meta.json")["templates"]))
            self.assertTrue(all(a["coming_soon"] and "template_release" not in a for a in catalog["apps"]))

    def test_packages_have_resolved_ports_and_no_client_hooks(self):
        for app in market.definitions():
            if not app["package"]:
                continue
            with self.subTest(app=app["template"]), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "package"
                config = market.render(ROOT, app, destination)
                self.assertEqual(config["appId"], "template-" + app["template"])
                self.assertNotIn("hooks", config)
                self.assertTrue(any(e["type"] == "http" for e in config["entitlements"]))
                for path in destination.rglob("*"):
                    if path.is_file():
                        try:
                            text = path.read_text()
                        except UnicodeDecodeError:
                            continue
                        self.assertNotRegex(text, r"{{\s*\.[A-Z_]+\s*}}", str(path))

    def test_hardware_apps_cannot_accidentally_be_packaged(self):
        app = next(a for a in market.definitions() if a["template"] == "camera-feed")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                market.render(ROOT, app, Path(temporary) / "package")

    def test_promotion_requires_image_and_config_bound_platform_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store").mkdir()
            (root / "meta.json").write_bytes((ROOT / "meta.json").read_bytes())
            (root / "store/apps.json").write_bytes((ROOT / "store/apps.json").read_bytes())
            config = json.dumps({"appId": "template-simple-api", "platform": "linux",
                                 "entitlements": [{"type": "http", "port": 3001}]})
            image = "ghcr.io/wendylabsinc/template-simple-api@sha256:" + "a" * 64
            release = {"image_reference": image, "app_config_json": config,
                       "source_revision": "b" * 40, "platforms": ["linux/amd64", "linux/arm64"],
                       "qualification": {}}
            path = root / "store/releases.json"
            def write():
                path.write_text(json.dumps({"releases": {"template-simple-api": release}}))
            write()
            with self.assertRaises((ValueError, KeyError)):
                market.catalog(root)
            for platform in release["platforms"]:
                release["qualification"][platform] = {
                    "image_reference": image,
                    "app_config_sha256": hashlib.sha256(config.encode()).hexdigest(),
                    "evidence_url": "https://example.com/device-qualification",
                    "install_passed": True, "health_passed": True,
                }
            write()
            self.assertFalse(market.catalog(root)["apps"][3]["coming_soon"])
            release["platforms"] = ["linux/arm64"]
            write()
            published = market.catalog(root)["apps"][3]["template_release"]
            self.assertEqual(published["platforms"], ["linux/arm64"])
            release["app_config_json"] = config + " "
            write()
            with self.assertRaises(ValueError):
                market.catalog(root)


if __name__ == "__main__":
    unittest.main()
