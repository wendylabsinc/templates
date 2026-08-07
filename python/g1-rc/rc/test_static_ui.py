from pathlib import Path
import unittest


class VelocityLoopSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent / "web" / "index.html").read_text()

    def test_page_load_does_not_send_zero_velocity(self):
        self.assertIn("let lastSentZero = true", self.html)

    def test_velocity_requests_are_serialized(self):
        self.assertIn("let velocityInFlight = false", self.html)
        self.assertIn("if (!readyToWalk || velocityInFlight) return", self.html)

    def test_status_requests_are_serialized(self):
        self.assertIn("let statusInFlight = false", self.html)
        self.assertIn("if (statusInFlight) return", self.html)

    def test_controls_use_one_action_panel(self):
        self.assertEqual(self.html.count('class="actions action-panel"'), 1)

    def test_drive_controls_use_a_separate_deck(self):
        self.assertIn('class="drive-deck"', self.html)
        self.assertIn('class="workspace"', self.html)
        self.assertIn('/static/wendy.css', self.html)

    def test_page_uses_wendy_brand(self):
        self.assertIn('/static/wendy-logo.svg', self.html)
        css = (Path(__file__).parent / "web" / "wendy.css").read_text()
        self.assertIn("--brand: #9fe2bf", css)
        self.assertIn("#stop {\n  position: static", css)

    def test_static_assets_are_mounted_from_the_real_directory(self):
        server = (Path(__file__).parent / "main.py").read_text()
        self.assertIn("StaticFiles(directory=STATIC_DIR)", server)
        self.assertNotIn('STATIC_DIR / "static"', server)


if __name__ == "__main__":
    unittest.main()
