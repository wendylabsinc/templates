from pathlib import Path
import unittest


class VelocityLoopSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parent / "web" / "index.html").read_text()
        cls.server = (Path(__file__).parent / "main.py").read_text()
        template_root = Path(__file__).parents[1]
        cls.motion_server = (template_root / "motion" / "main.py").read_text()
        cls.camera_server = (template_root / "camera" / "main.py").read_text()

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
        self.assertIn("StaticFiles(directory=STATIC_DIR)", self.server)
        self.assertNotIn('STATIC_DIR / "static"', self.server)

    def test_container_copies_web_assets_explicitly(self):
        dockerfile = (Path(__file__).parent / "Dockerfile").read_text()
        self.assertIn(
            "COPY web/index.html web/wendy.css web/wendy-logo.svg ./web/",
            dockerfile,
        )
        self.assertNotIn("COPY web ./web", dockerfile)

    def test_ui_exposes_first_run_diagnostics(self):
        self.assertIn('id="diagnostics-panel"', self.html)
        self.assertIn('id="problem-banner"', self.html)
        self.assertIn('getJSON("/api/diagnostics")', self.html)
        self.assertIn("function reportProblem", self.html)
        self.assertIn("console.error(`[g1-rc] ${text}`)", self.html)

    def test_rc_aggregates_motion_and_camera_diagnostics(self):
        self.assertIn('@app.get("/api/diagnostics")', self.server)
        self.assertIn('await _motion_get("/diagnostics")', self.server)
        self.assertIn("CAMERA_DIAGNOSTICS_URL", self.server)
        self.assertIn("upstream_failed service=%s", self.server)

    def test_motion_stays_observable_after_startup_failure(self):
        self.assertIn('startup.update(\n            status="failed"', self.motion_server)
        self.assertIn('@app.get("/diagnostics")', self.motion_server)
        self.assertIn('"code": "motion_not_ready"', self.motion_server)
        self.assertIn("logger.exception(\n            \"startup stage=failed", self.motion_server)

    def test_camera_reports_actionable_capture_failures(self):
        self.assertIn('"camera_open_failed"', self.camera_server)
        self.assertIn('"camera_read_failed"', self.camera_server)
        self.assertIn('@app.get("/diagnostics")', self.camera_server)
        self.assertIn("recent_events.snapshot()", self.camera_server)


if __name__ == "__main__":
    unittest.main()
