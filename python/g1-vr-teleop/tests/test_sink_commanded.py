"""What the sink reports as commanded is what it queued, after the arming offset."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))


def test_the_sink_reports_what_it_queued_not_what_it_was_given():
    """apply() subtracts the arming offset before anything leaves the sink.

    Measured at 53.8 deg on an elbow. Anything comparing a command with a
    measured angle must use the post-offset value or it sees that offset as a
    permanent error on a joint nothing is touching.
    """
    import sink_realg1
    s = sink_realg1.RealG1Sink.__new__(sink_realg1.RealG1Sink)
    s._lock = __import__("threading").Lock()
    s._latest = {"arm_partial": {"left_elbow_joint": 0.25}}
    assert s.commanded() == {"left_elbow_joint": 0.25}
    s._latest = {}
    assert s.commanded() is None
