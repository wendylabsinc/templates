# camera-feed-yolo-usb

YOLOv8 COCO object detection on a **USB camera**, inference on the CPU via ONNX
Runtime, with the annotated feed streamed to the browser over a WebSocket.

## When to use this instead of the other YOLO templates

| Template | Camera | Inference |
| --- | --- | --- |
| `camera-feed-yolo` | device camera via GStreamer | GPU on Jetson, CPU elsewhere |
| **`camera-feed-yolo-usb`** | **USB webcam via V4L2** | **CPU, ONNX Runtime** |

Reach for this one when the camera is a USB webcam and the device has no usable
GPU — a Qualcomm Dragonwing, a Raspberry Pi, any small Linux board.

## The three things it does differently

### It asks for MJPEG, and checks what it got

A USB 2.0 port carries 480 Mbit/s. Raw YUYV at 1280x720 and 30 fps needs about
440 Mbit/s and does not fit in practice, so V4L2 quietly drops the resolution
until it does — and you get a soft, blocky picture with no error anywhere to
explain it.

| Format | 1280x720 @30 | Fits in USB 2.0 |
| --- | ---: | --- |
| YUYV (raw) | ~440 Mbit/s | no |
| MJPEG | ~15 Mbit/s | comfortably |

OpenCV takes whichever format the camera lists first, which for most webcams is
the raw one. This template asks for `MJPG` **before** setting the frame size —
in the other order V4L2 sizes the raw format and ignores the codec change — and
`/status` reports what was actually negotiated, because V4L2 will accept a
request it has no intention of honouring.

If `/status` comes back `YUYV`, the camera genuinely cannot compress and the
resolution ceiling is real.

### It asks the driver which node is the camera

A UVC webcam claims more than one `/dev/video*` node and only the first
captures; the rest open successfully and never yield a frame. Some SoCs also
register their hardware video **codec** as a V4L2 device, which is not a camera
at all and cannot capture anything.

`_query()` uses `VIDIOC_QUERYCAP` to ask the driver, rather than assuming
`/dev/video0`.

### torch never reaches the runtime image

`ultralytics` pulls in torch — about 3 GB — and is needed exactly once, to
export `yolov8n.onnx`. That happens in a build stage that is discarded. The
runtime installs `onnxruntime` alone, and the image lands around 400 MB.

## Hardware

A USB webcam that supports MJPEG. Most do; check with:

```sh
v4l2-ctl --device /dev/video0 --list-formats-ext
```

On boards where the USB-C port is wired for gadget mode (USB networking), the
camera goes in whichever port is the **host** — often a micro-USB socket, and
often needing an OTG adapter. `/status` will say `"camera": null` with an
explanation if nothing is found.

## Run

```sh
wendy init --app-id my-detector --template camera-feed-yolo-usb --language python
cd my-detector
wendy run --device <device>
```

Then open `http://<device>:3007/`.

## Tuning

| Env | Default | |
| --- | --- | --- |
| `YOLO_DEVICE` | auto | force a `/dev/video*` node |
| `YOLO_IMGSZ` | 320 | must match the Dockerfile's export size |
| `YOLO_MAX_FPS` | 10 | inference rate; capture stays at 30 |
| `YOLO_CONF` | 0.25 | confidence threshold |
| `YOLO_WIDTH` / `YOLO_HEIGHT` | 1280x720 | capture size |

Changing `YOLO_IMGSZ` alone will not work — the ONNX graph is exported at a
fixed input size. Rebuild with `--build-arg YOLO_IMGSZ=<n>` to match.
