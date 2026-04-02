import subprocess
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


@router.get("/gpu")
def gpu_info():
    info = {"available": False}
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        if out:
            parts = [p.strip() for p in out.split(",")]
            info = {
                "available": True,
                "name": parts[0] if len(parts) > 0 else None,
                "memory": f"{parts[1]} MiB" if len(parts) > 1 else None,
                "driver": parts[2] if len(parts) > 2 else None,
                "temperature": f"{parts[3]}°C" if len(parts) > 3 else None,
            }
    except Exception:
        try:
            temp = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            info = {
                "available": True,
                "name": "ARM GPU",
                "temperature": f"{int(temp) / 1000:.1f}°C",
            }
        except Exception:
            pass
    return info
