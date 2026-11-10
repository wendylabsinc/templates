"""One certificate across deploys, or the headset stops talking after each one.

The Dockerfile generated the TLS cert with `RUN openssl ...` -- at BUILD time
-- so every deploy served a brand-new self-signed certificate. The headset has
to accept each one by hand, and a page whose certificate has not been accepted
cannot open a secure websocket or enter WebXR. So after a redeploy the headset
silently sent nothing, and the remedy looked like "reload the page": sometimes
that worked, because the operator hit the interstitial, and sometimes it did
not. It cost a dozen reconnect cycles in one evening before anyone looked at
the certificate.
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
# conftest's autouse fixture imports retarget_g1. This module needs none of
# it, but every module pays for it -- and relying on an alphabetically earlier
# test to have put bridge/ on the path is how test_hands_only broke when a
# file named test_dex3_order.py appeared.
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
ENTRY = ROOT / "teleop" / "entrypoint.sh"


def _run(tmp, env=None, fake_server=True):
    """Run entrypoint.sh with openssl real and the server replaced by echo."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    if fake_server:
        py = bin_dir / "python3"
        py.write_text("#!/bin/sh\necho SERVER_STARTED \"$@\"\n")
        py.chmod(0o755)
    e = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin",
         "TELEOP_RETARGET": "g1", "TELEOP_SINK": "realg1",
         "TELEOP_PORT": "8200", "TELEOP_TLS_PORT": "8443",
         "TELEOP_CERT_DIR": str(tmp / "certs")}
    e.update(env or {})
    return subprocess.run(["sh", str(ENTRY)], capture_output=True, text=True,
                          env=e, cwd=str(ROOT), timeout=60)


def _fingerprint(path):
    out = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256",
                          "-in", str(path)], capture_output=True, text=True)
    return out.stdout.strip()


def test_it_generates_a_certificate_when_there_is_none(tmp_path):
    r = _run(tmp_path)
    assert "generating" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "certs" / "cert.pem").is_file()
    assert "SERVER_STARTED" in r.stdout


def test_the_second_deploy_serves_the_SAME_certificate(tmp_path):
    """The whole point. A different certificate here is a headset that has to
    be re-accepted, and until it is, one that sends nothing."""
    _run(tmp_path)
    first = _fingerprint(tmp_path / "certs" / "cert.pem")
    r = _run(tmp_path)
    second = _fingerprint(tmp_path / "certs" / "cert.pem")
    assert first and first == second, "the certificate changed between deploys"
    assert "reusing" in r.stdout


def test_a_certificate_about_to_expire_is_replaced(tmp_path):
    """Stable is not the same as forever."""
    d = tmp_path / "certs"
    d.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "10", "-keyout", str(d / "key.pem"),
                    "-out", str(d / "cert.pem"), "-subj", "/CN=old"],
                   capture_output=True, check=True)
    old = _fingerprint(d / "cert.pem")
    r = _run(tmp_path)
    assert "generating" in r.stdout
    assert _fingerprint(d / "cert.pem") != old


def test_an_unwritable_directory_falls_back_rather_than_failing_to_start(tmp_path):
    """A bridge that will not start is worse than one asking for a warning."""
    r = _run(tmp_path, env={"TELEOP_CERT_DIR": "/proc/nope/certs"})
    assert "SERVER_STARTED" in r.stdout
    assert "/tmp/teleop-certs" in r.stdout


def test_the_validity_stays_under_the_limit_chromium_enforces(tmp_path):
    """Oculus Browser is Chromium: past 398 days it is ERR_CERT_VALIDITY_TOO_LONG,
    which -- unlike an untrusted issuer -- offers no way through. A ten-year
    certificate shows in the headset simply as "the page is not working"."""
    _run(tmp_path)
    out = subprocess.run(["openssl", "x509", "-noout", "-dates", "-in",
                          str(tmp_path / "certs" / "cert.pem")],
                         capture_output=True, text=True).stdout
    import datetime as dt
    fmt = "%b %d %H:%M:%S %Y %Z"
    d = {k: v for k, v in (l.split("=", 1) for l in out.strip().split("\n"))}
    span = dt.datetime.strptime(d["notAfter"].strip(), fmt) - \
        dt.datetime.strptime(d["notBefore"].strip(), fmt)
    assert span.days <= 398, f"{span.days} days; Chromium rejects over 398"


def test_the_dockerfile_no_longer_bakes_the_served_certificate(tmp_path):
    text = (ROOT / "teleop" / "Dockerfile").read_text()
    assert "/app/entrypoint.sh" in text
    assert "openssl req" not in text, \
        "the served certificate must come from the persist volume"
