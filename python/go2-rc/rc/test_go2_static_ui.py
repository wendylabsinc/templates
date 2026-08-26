from pathlib import Path
import re
import unittest


class Go2StaticUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parent
        cls.html = (root / "static" / "index.html").read_text()
        cls.server = (root / "main.py").read_text()

    def test_uses_wendy_brand_tokens_and_fonts(self):
        self.assertIn("--background: #171c23", self.html)
        self.assertIn("--cream: #f1eee7", self.html)
        self.assertIn("--seafoam: #9fe2bf", self.html)
        self.assertIn("family=Geist:wght@400;500", self.html)
        self.assertNotIn("#0b0d10", self.html)
        self.assertNotIn("#4ea7ff", self.html)

    def test_uses_square_controls_and_official_logo(self):
        radii = re.findall(r"border-radius:\s*([^;]+);", self.html)
        self.assertTrue(radii)
        self.assertTrue(all(value.strip() in {"0", "0px"} for value in radii))
        self.assertIn('/static/wendy-logo.svg', self.html)
        self.assertTrue((Path(__file__).parent / "static" / "wendy-logo.svg").is_file())

    def test_static_directory_is_mounted(self):
        self.assertIn("StaticFiles(directory=STATIC_DIR)", self.server)
        self.assertNotIn('STATIC_DIR / "static"', self.server)


if __name__ == "__main__":
    unittest.main()
