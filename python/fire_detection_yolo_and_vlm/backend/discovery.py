import logging
import socket

from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)


class AlbertDiscovery:
    def __init__(self, port: int = 5702):
        self.port = port
        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None

    def start(self):
        try:
            self.zeroconf = Zeroconf()
            hostname = socket.gethostname()
            # Get all non-loopback IPv4 addresses
            addresses = []
            try:
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                    addr = info[4][0]
                    if not addr.startswith("127."):
                        addresses.append(socket.inet_aton(addr))
            except socket.gaierror:
                pass

            if not addresses:
                # Fallback: bind to all interfaces
                addresses = [socket.inet_aton("0.0.0.0")]

            self.service_info = ServiceInfo(
                "_albert._tcp.local.",
                name=f"Albert on {hostname}._albert._tcp.local.",
                addresses=addresses,
                port=self.port,
                properties={"version": "0.1.0"},
                server=f"{hostname}.local.",
            )
            self.zeroconf.register_service(self.service_info)
        except Exception as exc:
            logger.warning(
                "Albert mDNS registration unavailable: %s: %s",
                type(exc).__name__,
                exc,
            )
            if self.zeroconf:
                self.zeroconf.close()
            self.zeroconf = None
            self.service_info = None

    def stop(self):
        if self.zeroconf and self.service_info:
            self.zeroconf.unregister_service(self.service_info)
        if self.zeroconf:
            self.zeroconf.close()
            self.zeroconf = None
