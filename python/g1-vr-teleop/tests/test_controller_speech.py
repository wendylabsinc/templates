"""Voice prompts: removed from the headset, kept (unreachable) onboard.

The right controller's A/B used to speak two fixed bartender prompts through
the G1's speaker. They were removed on the operator's request: A/B do nothing
now, and the legend no longer lists them. The onboard's allowlisted `speak`
command is unchanged -- removing it would need an onboard deploy for no
safety gain, since nothing on the bridge sends it any more.
"""
import pathlib
import inspect
import sys

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))

import g1_onboard as onboard  # noqa: E402
import server  # noqa: E402


def test_the_bridge_no_longer_speaks_on_a_or_b():
    assert not hasattr(server, "speak_on_face_button")
    assert not hasattr(server, "VOICE_PROMPT_BUTTONS")
    source = inspect.getsource(server.ws_handler)
    assert "speak" not in source


def test_the_headset_legend_has_no_voice_row():
    hud = (ROOT / "teleop" / "web" / "hud.js").read_text()
    legend = hud.split("const rows = [")[1].split("];")[0]
    assert "A / B" not in legend and "thank you" not in legend.lower()


class _Audio:
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def TtsMaker(self, text, speaker_id):
        self.calls.append((text, speaker_id))
        return self.code


def test_onboard_uses_the_fixed_english_tts_prompts():
    audio = _Audio()
    cmd = onboard.Commander.__new__(onboard.Commander)
    cmd.audio = audio
    assert cmd.speak("drink_question") == "speaking: What would you like to drink?"
    assert cmd.speak("thank_you") == "speaking: Thank you!"
    assert audio.calls == [
        ("What would you like to drink?", 1),
        ("Thank you!", 1),
    ]


def test_onboard_refuses_arbitrary_text_and_reports_audio_failures():
    cmd = onboard.Commander.__new__(onboard.Commander)
    cmd.audio = _Audio(code=7)
    assert "unknown prompt" in cmd.speak("say_anything")
    assert "failed" in cmd.speak("thank_you")
    cmd.audio = None
    assert "unavailable" in cmd.speak("thank_you")


def test_speech_is_queued_off_the_websocket_control_loop():
    cmd = onboard.Commander.__new__(onboard.Commander)
    cmd._speech_queue = __import__("queue").Queue(maxsize=1)
    assert cmd.queue_speech("thank_you") == "speech queued: Thank you!"
    assert cmd._speech_queue.get_nowait() == "thank_you"
    assert "unknown prompt" in cmd.queue_speech("arbitrary_text")
