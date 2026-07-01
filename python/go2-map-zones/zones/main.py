#!/usr/bin/env python3
"""go2-map-zones -- a browser front-end for offline occupancy-map zone segmentation.

Upload an occupancy map (either our own .npz archive, or a nav2 map_saver .pgm + .yaml pair),
run the watershed segmenter, and get back the labelled zones plus a rendered visualization. No
robot connection is involved — this is a pure map-processing utility that pairs with whatever
SLAM / mapping stack produced the map.

Endpoints:
  GET  /                          → the single-page UI
  GET  /health                    → {"status": "ok", "output_dir": ...}
  POST /api/segment               → multipart upload (.npz OR .pgm + .yaml) + optional params
                                     → {id, count, zones, viz_url, yaml_url}
  GET  /api/result/{id}/viz.png   → the rendered PNG for a segmentation
  GET  /api/result/{id}/yaml      → the zones.yaml (JSON) for a segmentation
"""

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import segmenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("go2-map-zones")

PORT = int(os.environ.get("PORT", "3001"))
STATIC_DIR = Path(__file__).parent / "static"

# Where segmentation outputs land. The WendyOS volume mount is /data; fall back to a local dir
# if that isn't writable (e.g. running the container standalone for a quick test).
_PREFERRED_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/data/segmentations")


def _pick_output_dir() -> Path:
    for candidate in (_PREFERRED_OUTPUT_DIR, "/app/out"):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            # confirm writability
            probe = p / ".write_test"
            probe.touch()
            probe.unlink()
            return p
        except OSError:
            log.warning("output dir %s not writable; trying fallback", candidate)
    raise RuntimeError("no writable output directory available")


OUTPUT_DIR = _pick_output_dir()
log.info("go2-map-zones up. output_dir=%s port=%d", OUTPUT_DIR, PORT)

app = FastAPI(title="go2-map-zones", version="0.1.0")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "output_dir": str(OUTPUT_DIR)})


def _save_upload(upload: UploadFile, dest: Path) -> None:
    """Stream an UploadFile to disk (avoids loading a whole map into memory)."""
    with open(dest, "wb") as out:
        shutil.copyfileobj(upload.file, out)


@app.post("/api/segment")
async def segment(
    npz: UploadFile | None = File(default=None),
    pgm: UploadFile | None = File(default=None),
    yaml: UploadFile | None = File(default=None),
    core_dist_m: float = Form(default=1.6),
    min_area_m2: float = Form(default=6.0),
    island_max_m2: float = Form(default=2.5),
) -> JSONResponse:
    """Segment an uploaded occupancy map into zones.

    Provide EITHER a single `.npz`, OR both `pgm` and `yaml` (a nav2 map_saver pair). The three
    tuning params are optional and default to the segmenter's own defaults.
    """
    if npz is None and not (pgm is not None and yaml is not None):
        raise HTTPException(
            400, "upload either an .npz map, or both a .pgm and its .yaml (nav2 map pair)"
        )

    seg_id = uuid.uuid4().hex[:12]
    out_dir = OUTPUT_DIR / seg_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Uploads are streamed into a scratch dir, parsed into the common occupancy tuple, then
    # discarded — only the derived zones.yaml + viz.png persist under the output dir.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            if npz is not None:
                npz_path = tmp_path / "map.npz"
                _save_upload(npz, npz_path)
                grid, res, ox, oy = segmenter.load_npz(str(npz_path))
                source = npz.filename or "map.npz"
            else:
                pgm_path = tmp_path / "map.pgm"
                yaml_path = tmp_path / "map.yaml"
                _save_upload(pgm, pgm_path)
                _save_upload(yaml, yaml_path)
                grid, res, ox, oy = segmenter.load_nav2_map(str(pgm_path), str(yaml_path))
                source = pgm.filename or "map.pgm"
        except (KeyError, ValueError, FileNotFoundError, OSError) as exc:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise HTTPException(400, f"could not read the uploaded map: {exc}") from exc

    try:
        zones, markers = segmenter.segment_grid(
            grid,
            res,
            ox,
            oy,
            core_dist_m=core_dist_m,
            min_area_m2=min_area_m2,
            island_max_m2=island_max_m2,
        )
    except Exception as exc:  # segmentation is CV-heavy; surface any failure cleanly
        shutil.rmtree(out_dir, ignore_errors=True)
        log.exception("segmentation failed")
        raise HTTPException(500, f"segmentation failed: {exc}") from exc

    # Strip the internal watershed label before persisting / returning — it's an implementation
    # detail of the viz, not part of the zone contract.
    clean = [{k: v for k, v in z.items() if k != "_label"} for z in zones]
    yaml_out = out_dir / "zones.yaml"
    with open(yaml_out, "w") as f:
        json.dump({"source": source, "zones": clean}, f, indent=2)
    viz_out = out_dir / "zones_viz.png"
    segmenter._viz(grid, markers, zones, res, ox, oy, str(viz_out))

    log.info("segmented %s -> %d zones (%s)", source, len(zones), seg_id)
    return JSONResponse(
        {
            "id": seg_id,
            "count": len(clean),
            "zones": clean,
            "viz_url": f"/api/result/{seg_id}/viz.png",
            "yaml_url": f"/api/result/{seg_id}/yaml",
        }
    )


@app.get("/api/result/{seg_id}/viz.png")
async def result_viz(seg_id: str) -> FileResponse:
    path = OUTPUT_DIR / _safe_id(seg_id) / "zones_viz.png"
    if not path.is_file():
        raise HTTPException(404, "no visualization for that id")
    return FileResponse(path, media_type="image/png")


@app.get("/api/result/{seg_id}/yaml")
async def result_yaml(seg_id: str) -> FileResponse:
    path = OUTPUT_DIR / _safe_id(seg_id) / "zones.yaml"
    if not path.is_file():
        raise HTTPException(404, "no zones file for that id")
    return FileResponse(path, media_type="application/json", filename="zones.yaml")


def _safe_id(seg_id: str) -> str:
    """Reject anything that isn't a bare hex id so a crafted id can't escape OUTPUT_DIR."""
    if not seg_id.isalnum():
        raise HTTPException(400, "invalid id")
    return seg_id


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    target = STATIC_DIR / "index.html"
    if not target.is_file():
        raise HTTPException(500, f"index.html missing at {target}")
    return FileResponse(target, media_type="text/html")


if (STATIC_DIR).is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
