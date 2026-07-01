"""CSI ingest sources.

``CSISource`` is the seam where transports plug in. v1 ships ``UDPCSISource``;
MQTT/TCP can be added by implementing the same interface without touching the
pipeline or DSP.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator


class CSISource(abc.ABC):
    """An async source of raw CSI payloads (one record per item)."""

    @abc.abstractmethod
    async def start(self) -> None:
        ...

    @abc.abstractmethod
    def frames(self) -> AsyncIterator[bytes]:
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: "asyncio.Queue[bytes]"):
        self._queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # drop under backpressure rather than block the event loop


class UDPCSISource(CSISource):
    """Receives CSI payloads as UDP datagrams on ``port``."""

    def __init__(self, port: int, host: str = "0.0.0.0", maxsize: int = 8192):
        self.port = port
        self.host = host
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=maxsize)
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self._queue),
            local_addr=(self.host, self.port),
        )

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
