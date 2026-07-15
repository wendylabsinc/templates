#!/usr/bin/env python3
import asyncio
import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse
from bleak import BleakScanner
from pydantic import BaseModel

app = FastAPI()


class BluetoothDevice(BaseModel):
    name: str | None
    address: str
    rssi: int | None


async def discover_devices(timeout: float = 5.0) -> list[BluetoothDevice]:
    """Discover Bluetooth devices for the specified timeout."""
    devices = await BleakScanner.discover(timeout=timeout)
    return [
        BluetoothDevice(
            name=device.name,
            address=device.address,
            rssi=device.rssi
        )
        for device in devices
    ]


@app.get("/discovered")
async def get_discovered_devices():
    """Return a JSON array of all discovered Bluetooth devices within 5 seconds."""
    devices = await discover_devices(timeout=5.0)
    return [device.model_dump() for device in devices]


@app.get("/events")
async def device_events():
    """SSE endpoint that streams discovered devices in real-time."""
    async def event_generator():
        detection_callback_queue: asyncio.Queue = asyncio.Queue()

        def detection_callback(device, advertisement_data):
            detection_callback_queue.put_nowait({
                "name": device.name,
                "address": device.address,
                "rssi": advertisement_data.rssi
            })

        async with BleakScanner(detection_callback=detection_callback):
            start_time = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= 30:  # Stop after 30 seconds
                    break
                try:
                    device = await asyncio.wait_for(
                        detection_callback_queue.get(),
                        timeout=1.0
                    )
                    yield {
                        "event": "device",
                        "data": json.dumps(device)
                    }
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {
                        "event": "keepalive",
                        "data": ""
                    }

    return EventSourceResponse(event_generator())


@app.get("/")
async def root():
    """Serve the index.html file."""
    index_path = Path(__file__).parent / "index.html"
    return FileResponse(index_path, media_type="text/html")


@app.get("/logo.svg")
async def logo():
    """Serve the logo.svg file."""
    logo_path = Path(__file__).parent / "logo.svg"
    return FileResponse(logo_path, media_type="image/svg+xml")
