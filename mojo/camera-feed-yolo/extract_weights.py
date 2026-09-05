# Build-time only (Dockerfile weights stage): pull the ultralytics YOLOv8n
# checkpoint, fold BatchNorm into the convs, and dump plain fp32 numpy
# tensors + the COCO class names. MAX cannot ingest the .pt or its ONNX
# export (docs/mojo-max-port-findings.md MMF-002), so the runtime image
# carries this torch-free archive and model.py rebuilds the network as a
# MAX graph from it.
import sys

import numpy as np
from ultralytics import YOLO

OUT = sys.argv[1] if len(sys.argv) > 1 else "/out"

m = YOLO("yolov8n.pt")
model = m.model.fuse().eval()

sd = {k: v.detach().numpy().astype(np.float32) for k, v in model.state_dict().items()}
assert len(sd) == 127, f"unexpected tensor count {len(sd)} — YOLOv8n layout changed?"
np.savez(f"{OUT}/weights.npz", **sd)

names = [model.names[i] for i in range(len(model.names))]
assert len(names) == 80
with open(f"{OUT}/names.txt", "w") as f:
    f.write("\n".join(names) + "\n")

print(f"extracted {len(sd)} tensors + {len(names)} class names to {OUT}")
