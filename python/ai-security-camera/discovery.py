#!/usr/bin/env python3
"""
Camera auto-discovery for the AI Security Camera app.

Two complementary, dependency-free mechanisms (uses only the stdlib + OpenCV,
which the app already bundles):

1. ONVIF WS-Discovery — a UDP multicast probe that ONVIF cameras answer with
   their device service address. Finds cameras regardless of subnet.
2. Port-554 subnet sweep — for cameras that don't answer WS-Discovery (e.g.
   ONVIF disabled but RTSP enabled), scan the local /24 for open RTSP ports.

For each candidate IP we then probe a list of well-known per-brand RTSP paths
and keep the first URL that actually yields a video frame. Credentials come
from CAMERA_USER / CAMERA_PASS.

NOTE: discovery only finds cameras that already hold an IP on the network. A
camera on a bare direct cable with no DHCP server has no address to discover —
assign it a static IP first (see the README).
"""

import os
import re
import socket
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2

logger = logging.getLogger('ai-security-camera.discovery')

WSD_ADDR = "239.255.255.250"
WSD_PORT = 3702

# Common RTSP stream paths by brand. We try each against a discovered IP and
# keep the first that returns a frame. {u}/{p}/{ip} are filled in at probe time.
RTSP_PATH_TEMPLATES = [
    "/h264Preview_01_main",                              # Reolink (main)
    "/h264Preview_01_sub",                               # Reolink (sub)
    "/Streaming/Channels/101",                           # Hikvision
    "/cam/realmonitor?channel=1&subtype=0",              # Amcrest / Dahua
    "/cam/realmonitor?channel=1&subtype=1",              # Amcrest / Dahua (sub)
    "/axis-media/media.amp",                             # Axis
    "/live/ch00_0",                                      # generic
    "/stream1",                                          # ONVIF generic / Tapo
    "/stream2",
    "/live",
    "/11",                                               # Dahua short form
    "/video1",
    "/0",
]


def _ws_discovery(timeout=4.0):
    """Send an ONVIF WS-Discovery probe and return the set of responding IPs."""
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<e:Header><w:MessageID>uuid:%s</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">'
        'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    ) % uuid.uuid4()

    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        s.settimeout(timeout)
        s.sendto(probe.encode(), (WSD_ADDR, WSD_PORT))
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                break
            ips.add(addr[0])
            # XAddrs may advertise a different IP than the responder.
            for url in re.findall(r'https?://([0-9.]+)', data.decode(errors='replace')):
                ips.add(url)
        s.close()
    except OSError as e:
        logger.warning(f"WS-Discovery failed: {e}")
    if ips:
        logger.info(f"WS-Discovery found: {sorted(ips)}")
    return ips


def _default_subnet_base():
    """Best-effort primary IPv4 subnet base ('a.b.c.') via the default route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip.rsplit(".", 1)[0] + "."
    except OSError:
        return None


def _port_open(ip, port=554, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _scan_subnet_554(extra_bases=None, timeout=0.4):
    """Scan local /24 subnet(s) for hosts with port 554 open."""
    bases = set()
    base = _default_subnet_base()
    if base and not base.startswith("169.254."):  # skip the huge link-local space
        bases.add(base)
    for b in (extra_bases or []):
        bases.add(b)

    found = set()
    lock = threading.Lock()

    def check(ip):
        if _port_open(ip, 554, timeout):
            with lock:
                found.add(ip)

    targets = [f"{b}{h}" for b in bases for h in range(1, 255)]
    if not targets:
        return found
    logger.info(f"Scanning {len(targets)} addresses for open RTSP (port 554)…")
    with ThreadPoolExecutor(max_workers=128) as pool:
        pool.map(check, targets)
    if found:
        logger.info(f"Port-554 scan found: {sorted(found)}")
    return found


def _probe_rtsp(ip, user, password, open_timeout_ms=4000):
    """Try known RTSP paths on ip; return the first URL that yields a frame."""
    cred = f"{user}:{password}@" if user else ""
    # Force TCP + bounded socket timeout so dead paths fail fast.
    os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = (
        f'rtsp_transport;tcp|stimeout;{open_timeout_ms * 1000}'
    )
    for path in RTSP_PATH_TEMPLATES:
        url = f"rtsp://{cred}{ip}:554{path}"
        cap = None
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if cap.isOpened() and cap.read()[0]:
                logger.info(f"✓ Working RTSP path on {ip}: {path}")
                return url
        except Exception:
            pass
        finally:
            if cap is not None:
                cap.release()
    logger.warning(f"No known RTSP path worked on {ip} "
                   f"(check credentials, or that RTSP is enabled)")
    return None


def discover_cameras(user=None, password=None, scan_554=True, timeout=4.0):
    """Discover cameras on the network and resolve their RTSP URLs.

    Returns a list of {'name', 'url', 'enabled'} dicts.
    """
    user = user if user is not None else os.environ.get('CAMERA_USER', 'admin')
    password = password if password is not None else os.environ.get('CAMERA_PASS', '')

    ips = set(_ws_discovery(timeout))
    if scan_554:
        extra = {ip.rsplit('.', 1)[0] + '.' for ip in ips}  # also sweep ONVIF hits' /24
        ips |= _scan_subnet_554(extra_bases=extra)

    if not ips:
        logger.warning("No cameras discovered on the network.")
        return []

    cameras = []
    for i, ip in enumerate(sorted(ips)):
        url = _probe_rtsp(ip, user, password)
        if url:
            cameras.append({'name': f'camera-{ip.replace(".", "-")}', 'url': url, 'enabled': True})
    logger.info(f"Discovery resolved {len(cameras)} camera stream(s).")
    return cameras


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    for cam in discover_cameras():
        print(cam['name'], '->', re.sub(r'://[^@]+@', '://***@', cam['url']))
