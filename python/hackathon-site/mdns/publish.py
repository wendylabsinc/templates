"""Publish short mDNS aliases (e.g. go2.local) for the hackathon page.

Runs on the jetson's HOST network (wendy `network: host` entitlement), so
it can answer mDNS queries on the LAN directly. It advertises one or more
memorable `<name>.local` names pointing at the jetson's own IP, alongside
the device's existing wendyos-jetson-2.local — so attendees can just type
http://go2.local instead of the long hostname.

We coexist with the host's avahi responder: we only claim NEW names
(go2/hackathon), never the host's own, so there's no name conflict. If the
jetson's IP changes (roams networks), we re-advertise.
"""
import os
import socket
import sys
import time

from zeroconf import ServiceInfo, Zeroconf

ALIASES = [a.strip() for a in os.environ.get("MDNS_ALIASES", "go2,hackathon").split(",") if a.strip()]
PORT = int(os.environ.get("MDNS_PORT", "80"))


def primary_ip() -> str:
    """The jetson's primary LAN IPv4 (the egress interface's address).

    Uses a connected UDP socket — no packets are actually sent; the kernel
    just resolves which local address would be used to reach the target.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def build_infos(ip: str):
    infos = []
    for name in ALIASES:
        infos.append(
            ServiceInfo(
                "_http._tcp.local.",
                f"{name}._http._tcp.local.",
                addresses=[socket.inet_aton(ip)],
                port=PORT,
                server=f"{name}.local.",
            )
        )
    return infos


def register(zc: Zeroconf, infos) -> None:
    for info in infos:
        # allow_name_change=False: our names are unique, so fail loudly
        # rather than silently becoming go2-2.local.
        zc.register_service(info, allow_name_change=False)
        print(f"[mdns] {info.server.rstrip('.')} -> {socket.inet_ntoa(info.addresses[0])}:{PORT}", flush=True)


def unregister(zc: Zeroconf, infos) -> None:
    for info in infos:
        try:
            zc.unregister_service(info)
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    ip = primary_ip()
    print(f"[mdns] advertising {ALIASES} -> {ip}:{PORT}", flush=True)
    zc = Zeroconf()
    infos = build_infos(ip)
    register(zc, infos)

    try:
        while True:
            time.sleep(15)
            try:
                cur = primary_ip()
            except Exception:  # noqa: BLE001
                continue
            if cur != ip:
                print(f"[mdns] IP changed {ip} -> {cur}; re-advertising", flush=True)
                unregister(zc, infos)
                ip = cur
                infos = build_infos(ip)
                register(zc, infos)
    except KeyboardInterrupt:
        pass
    finally:
        unregister(zc, infos)
        zc.close()


if __name__ == "__main__":
    sys.exit(main())
