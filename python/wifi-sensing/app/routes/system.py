import os
import platform
import shutil
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


@router.get("/system")
def system_info():
    hostname = os.environ.get("WENDY_HOSTNAME", platform.node())

    mem = {}
    try:
        mi = Path("/proc/meminfo").read_text()
        for line in mi.splitlines():
            if line.startswith("MemTotal"):
                mem["total"] = f"{int(line.split()[1]) // 1024} MB"
            elif line.startswith("MemAvailable"):
                mem["free"] = f"{int(line.split()[1]) // 1024} MB"
        if "total" in mem and "free" in mem:
            total = int(mem["total"].split()[0])
            free = int(mem["free"].split()[0])
            mem["used"] = f"{total - free} MB"
    except Exception:
        pass

    disk = {}
    try:
        usage = shutil.disk_usage("/")
        disk = {
            "total": f"{usage.total // (1024**3)} GB",
            "used": f"{usage.used // (1024**3)} GB",
            "free": f"{usage.free // (1024**3)} GB",
        }
    except Exception:
        pass

    cpu = {}
    try:
        ci = Path("/proc/cpuinfo").read_text()
        models = [
            l.split(":")[1].strip()
            for l in ci.splitlines()
            if l.startswith("model name")
        ]
        cpu = {
            "model": models[0] if models else platform.processor(),
            "cores": os.cpu_count() or 0,
        }
    except Exception:
        cpu = {"model": platform.processor(), "cores": os.cpu_count() or 0}

    uptime = ""
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        uptime = f"{h}h {m}m"
    except Exception:
        pass

    return {
        "hostname": hostname,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "uptime": uptime,
        "memory": mem,
        "disk": disk,
        "cpu": cpu,
    }
