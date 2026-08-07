from pathlib import Path
import unittest


class VelocityLoopSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent / "static" / "index.html").read_text()

    def test_page_load_does_not_send_zero_velocity(self):
        self.assertIn("let lastSentZero = true", self.html)

    def test_velocity_requests_are_serialized(self):
        self.assertIn("let velocityInFlight = false", self.html)
        self.assertIn("if (!readyToWalk || velocityInFlight) return", self.html)


if __name__ == "__main__":
    unittest.main()
