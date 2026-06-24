# mDNS alias responder — publishes go2.local / hackathon.local on the
# jetson's host network. slim (not alpine) so pip pulls prebuilt arm64
# wheels for zeroconf instead of needing a compiler.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "zeroconf>=0.131,<1.0"
COPY publish.py .

ENV MDNS_ALIASES={{.MDNS_ALIASES}}
CMD ["python", "publish.py"]
