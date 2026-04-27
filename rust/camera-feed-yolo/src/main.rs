use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::StatusCode,
    response::{Html, IntoResponse, Json},
    routing::get,
    Router,
};
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use ndarray::{Array, Axis};
use ort::execution_providers::{CPUExecutionProvider, CUDAExecutionProvider};
use ort::session::{builder::GraphOptimizationLevel, Session};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, RwLock,
};
use std::time::{Duration, Instant};
use tokio::sync::{broadcast, watch};

const INDEX_HTML: &str = include_str!("../index.html");

const COCO_NAMES: [&str; 80] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
];

const INPUT_SIZE: u32 = 640;

// ---------------------------------------------------------------------------
// MJPEGCamera — owns the GStreamer pipeline. Pushes raw JPEG frames into a
// broadcast channel for browser clients and into a `watch` slot for the
// inference task.
// ---------------------------------------------------------------------------

struct MJPEGCamera {
    pipeline: Option<gstreamer::Pipeline>,
    device: String,
    use_passthrough: bool,
    last_frame_at: Arc<Mutex<Option<Instant>>>,
    has_frames: Arc<AtomicBool>,
}

impl MJPEGCamera {
    fn new(use_passthrough: bool) -> Self {
        Self {
            pipeline: None,
            device: "/dev/video0".to_string(),
            use_passthrough,
            last_frame_at: Arc::new(Mutex::new(None)),
            has_frames: Arc::new(AtomicBool::new(false)),
        }
    }

    fn start(
        &mut self,
        device: &str,
        frames_tx: broadcast::Sender<Vec<u8>>,
        latest_tx: watch::Sender<Option<Vec<u8>>>,
    ) {
        self.stop();
        self.device = device.to_string();

        // Passthrough on RPi/CPU avoids a 30fps decode/re-encode brown-out under
        // GStreamer + inference load. Jetson keeps the decode/encode for quality
        // since it has hardware JPEG codecs.
        let inner = if self.use_passthrough {
            "image/jpeg ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        } else {
            "image/jpeg ! jpegdec ! jpegenc quality=85 ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        };
        let launch = format!("v4l2src device={device} ! {inner}");

        let element = match gstreamer::parse::launch(&launch) {
            Ok(e) => e,
            Err(err) => {
                eprintln!("[gst] parse_launch failed: {err}");
                return;
            }
        };
        let pipeline = match element.dynamic_cast::<gstreamer::Pipeline>() {
            Ok(p) => p,
            Err(_) => {
                eprintln!("[gst] not a pipeline");
                return;
            }
        };

        let sink = pipeline
            .by_name("sink")
            .and_then(|el| el.dynamic_cast::<AppSink>().ok());
        let Some(sink) = sink else {
            eprintln!("[gst] sink element missing");
            return;
        };

        let last_frame_at = self.last_frame_at.clone();
        let has_frames = self.has_frames.clone();
        sink.set_callbacks(
            gstreamer_app::AppSinkCallbacks::builder()
                .new_sample(move |appsink| {
                    let sample = appsink
                        .pull_sample()
                        .map_err(|_| gstreamer::FlowError::Eos)?;
                    if let Some(buffer) = sample.buffer() {
                        let map = buffer
                            .map_readable()
                            .map_err(|_| gstreamer::FlowError::Error)?;
                        let bytes = map.as_slice().to_vec();
                        let _ = frames_tx.send(bytes.clone());
                        let _ = latest_tx.send(Some(bytes));
                        *last_frame_at.lock().unwrap() = Some(Instant::now());
                        has_frames.store(true, Ordering::Release);
                    }
                    Ok(gstreamer::FlowSuccess::Ok)
                })
                .build(),
        );

        if let Err(err) = pipeline.set_state(gstreamer::State::Playing) {
            eprintln!("[gst] set_state(Playing) failed: {err}");
            let _ = pipeline.set_state(gstreamer::State::Null);
            return;
        }
        println!("[gst] pipeline started on {device} (passthrough={})", self.use_passthrough);
        self.pipeline = Some(pipeline);
    }

    fn stop(&mut self) {
        if let Some(pipeline) = self.pipeline.take() {
            let _ = pipeline.set_state(gstreamer::State::Null);
        }
        *self.last_frame_at.lock().unwrap() = None;
        self.has_frames.store(false, Ordering::Release);
    }

    fn last_frame_at(&self) -> Option<Instant> {
        *self.last_frame_at.lock().unwrap()
    }
}

// ---------------------------------------------------------------------------
// YOLO inference (ONNX Runtime)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
struct Detection {
    x1: f32,
    y1: f32,
    x2: f32,
    y2: f32,
    conf: f32,
    cls: usize,
    name: &'static str,
}

struct YoloEngine {
    session: Session,
}

impl YoloEngine {
    fn new(use_gpu: bool) -> ort::Result<Self> {
        let mut builder = Session::builder()?;
        if use_gpu {
            builder = builder.with_execution_providers([
                CUDAExecutionProvider::default().build(),
                CPUExecutionProvider::default().build(),
            ])?;
            println!("[yolo] requesting CUDA execution provider");
        } else {
            builder = builder.with_execution_providers([CPUExecutionProvider::default().build()])?;
            println!("[yolo] using CPU execution provider");
        }
        let session = builder
            .with_optimization_level(GraphOptimizationLevel::Level3)?
            .with_intra_threads(2)?
            .commit_from_file("yolov8n.onnx")?;
        Ok(Self { session })
    }

    fn infer(&mut self, jpeg: &[u8], conf_threshold: f32) -> ort::Result<(Vec<Detection>, u32, u32)> {
        // Decode JPEG -> RGB.
        let image = match turbojpeg::decompress(jpeg, turbojpeg::PixelFormat::RGB) {
            Ok(img) => img,
            Err(err) => {
                eprintln!("[yolo] jpeg decode failed: {err}");
                return Ok((Vec::new(), 0, 0));
            }
        };
        let (w, h) = (image.width as u32, image.height as u32);
        let pixels = image.pixels;

        // Letterbox to INPUT_SIZE x INPUT_SIZE.
        let scale = (INPUT_SIZE as f32 / w as f32).min(INPUT_SIZE as f32 / h as f32);
        let new_w = (w as f32 * scale).round() as u32;
        let new_h = (h as f32 * scale).round() as u32;
        let pad_x = (INPUT_SIZE - new_w) / 2;
        let pad_y = (INPUT_SIZE - new_h) / 2;

        let mut input = Array::<f32, _>::from_elem((1, 3, INPUT_SIZE as usize, INPUT_SIZE as usize), 114.0 / 255.0);

        // Nearest-neighbour resize + paste; cheap and good enough at 640.
        let stride = (w * 3) as usize;
        for y in 0..new_h {
            let src_y = ((y as f32 + 0.5) / scale).floor() as u32;
            let src_y = src_y.min(h - 1);
            let row_off = src_y as usize * stride;
            for x in 0..new_w {
                let src_x = ((x as f32 + 0.5) / scale).floor() as u32;
                let src_x = src_x.min(w - 1);
                let idx = row_off + src_x as usize * 3;
                let r = pixels[idx] as f32 / 255.0;
                let g = pixels[idx + 1] as f32 / 255.0;
                let b = pixels[idx + 2] as f32 / 255.0;
                let py = (pad_y + y) as usize;
                let px = (pad_x + x) as usize;
                input[[0, 0, py, px]] = r;
                input[[0, 1, py, px]] = g;
                input[[0, 2, py, px]] = b;
            }
        }

        let input_value = ort::value::Tensor::from_array(input)?;
        let outputs = self.session.run(ort::inputs!["images" => input_value])?;
        let Some((_, output)) = outputs.iter().next() else {
            eprintln!("[yolo] inference returned no outputs");
            return Ok((Vec::new(), w, h));
        };
        let view = output.try_extract_tensor::<f32>()?.into_dimensionality::<ndarray::Ix3>()?;
        // YOLOv8 output shape: (1, 84, 8400). 4 box coords + 80 class scores.
        let preds = view.index_axis(Axis(0), 0); // (84, 8400)
        let n = preds.shape()[1];

        let mut candidates: Vec<Detection> = Vec::new();
        for i in 0..n {
            let mut best_cls = 0usize;
            let mut best_score = 0.0f32;
            for c in 0..80 {
                let s = preds[[4 + c, i]];
                if s > best_score {
                    best_score = s;
                    best_cls = c;
                }
            }
            if best_score < conf_threshold {
                continue;
            }
            // (cx, cy, w, h) in INPUT_SIZE space.
            let cx = preds[[0, i]];
            let cy = preds[[1, i]];
            let bw = preds[[2, i]];
            let bh = preds[[3, i]];
            let x1 = cx - bw * 0.5;
            let y1 = cy - bh * 0.5;
            let x2 = cx + bw * 0.5;
            let y2 = cy + bh * 0.5;
            // Undo letterbox + scale to original image coords.
            let ox1 = ((x1 - pad_x as f32) / scale).clamp(0.0, w as f32 - 1.0);
            let oy1 = ((y1 - pad_y as f32) / scale).clamp(0.0, h as f32 - 1.0);
            let ox2 = ((x2 - pad_x as f32) / scale).clamp(0.0, w as f32 - 1.0);
            let oy2 = ((y2 - pad_y as f32) / scale).clamp(0.0, h as f32 - 1.0);
            candidates.push(Detection {
                x1: ox1,
                y1: oy1,
                x2: ox2,
                y2: oy2,
                conf: best_score,
                cls: best_cls,
                name: COCO_NAMES[best_cls],
            });
        }

        // Sort by confidence desc, then class-aware NMS.
        candidates.sort_by(|a, b| b.conf.partial_cmp(&a.conf).unwrap_or(std::cmp::Ordering::Equal));
        let mut kept: Vec<Detection> = Vec::with_capacity(candidates.len());
        for cand in candidates {
            let drop = kept.iter().any(|k| k.cls == cand.cls && iou(k, &cand) > 0.45);
            if !drop {
                kept.push(cand);
            }
            if kept.len() >= 100 {
                break;
            }
        }

        Ok((kept, w, h))
    }
}

fn iou(a: &Detection, b: &Detection) -> f32 {
    let inter_x1 = a.x1.max(b.x1);
    let inter_y1 = a.y1.max(b.y1);
    let inter_x2 = a.x2.min(b.x2);
    let inter_y2 = a.y2.min(b.y2);
    let inter_w = (inter_x2 - inter_x1).max(0.0);
    let inter_h = (inter_y2 - inter_y1).max(0.0);
    let inter = inter_w * inter_h;
    let area_a = (a.x2 - a.x1).max(0.0) * (a.y2 - a.y1).max(0.0);
    let area_b = (b.x2 - b.x1).max(0.0) * (b.y2 - b.y1).max(0.0);
    let union = area_a + area_b - inter;
    if union <= 0.0 {
        0.0
    } else {
        inter / union
    }
}

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct AppState {
    camera: Arc<Mutex<MJPEGCamera>>,
    frames_tx: broadcast::Sender<Vec<u8>>,
    latest_tx: watch::Sender<Option<Vec<u8>>>,
    meta: Arc<RwLock<MetaState>>,
    confidence: Arc<RwLock<f32>>,
}

struct MetaState {
    json: String,
}

impl Default for MetaState {
    fn default() -> Self {
        Self {
            json: r#"{"detections":0,"inference_ms":0,"classes":{},"boxes":[],"frame_w":0,"frame_h":0}"#.to_string(),
        }
    }
}

// ---------------------------------------------------------------------------
// Watchdog: restart pipeline with backoff if it failed to start or stalled
// while clients are connected. Mirrors python's _restart_until_available.
// ---------------------------------------------------------------------------

fn spawn_watchdog(state: AppState) {
    tokio::spawn(async move {
        let stall_timeout = Duration::from_secs(2);
        let mut delay = Duration::from_millis(1000);
        loop {
            tokio::time::sleep(Duration::from_millis(500)).await;
            if state.frames_tx.receiver_count() == 0 {
                delay = Duration::from_millis(1000);
                continue;
            }

            let mut cam = state.camera.lock().unwrap();
            let pipeline_alive = cam.pipeline.is_some();
            let stalled = pipeline_alive
                && cam
                    .last_frame_at()
                    .map(|t| t.elapsed() > stall_timeout)
                    .unwrap_or(false);
            if stalled {
                eprintln!("[gst] pipeline stalled — restarting");
                cam.stop();
            }

            if cam.pipeline.is_none() {
                let device = cam.device.clone();
                cam.start(&device, state.frames_tx.clone(), state.latest_tx.clone());
                if cam.pipeline.is_some() {
                    delay = Duration::from_millis(1000);
                } else {
                    drop(cam);
                    eprintln!("[gst] retry in {}ms", delay.as_millis());
                    tokio::time::sleep(delay).await;
                    delay = (delay.mul_f32(1.5)).min(Duration::from_secs(5));
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Inference task
// ---------------------------------------------------------------------------

fn spawn_inference_task(
    state: AppState,
    mut latest_rx: watch::Receiver<Option<Vec<u8>>>,
    mut engine: YoloEngine,
    use_gpu: bool,
) {
    std::thread::spawn(move || {
        let min_interval = if use_gpu {
            Duration::from_millis(1000 / 15)
        } else {
            Duration::from_millis(1000 / 3)
        };
        let mut last_run = Instant::now() - min_interval;
        let rt = match tokio::runtime::Builder::new_current_thread().enable_time().build() {
            Ok(rt) => rt,
            Err(err) => {
                eprintln!("[yolo] inference runtime failed: {err}");
                return;
            }
        };
        rt.block_on(async move {
            loop {
                if latest_rx.changed().await.is_err() {
                    return;
                }
                let elapsed = last_run.elapsed();
                if elapsed < min_interval {
                    tokio::time::sleep(min_interval - elapsed).await;
                }
                let jpeg = match latest_rx.borrow_and_update().clone() {
                    Some(j) => j,
                    None => continue,
                };
                let conf = *state.confidence.read().unwrap();
                let started = Instant::now();
                let result = engine.infer(&jpeg, conf);
                let inference_ms = started.elapsed().as_secs_f64() * 1000.0;
                last_run = Instant::now();
                match result {
                    Ok((dets, w, h)) => {
                        let mut classes = serde_json::Map::new();
                        for d in &dets {
                            let entry = classes.entry(d.name.to_string()).or_insert_with(|| json!(0));
                            *entry = json!(entry.as_u64().unwrap_or(0) + 1);
                        }
                        let payload = json!({
                            "detections": dets.len(),
                            "inference_ms": (inference_ms * 10.0).round() / 10.0,
                            "classes": classes,
                            "boxes": dets,
                            "frame_w": w,
                            "frame_h": h,
                        });
                        if let Ok(s) = serde_json::to_string(&payload) {
                            state.meta.write().unwrap().json = s;
                        }
                    }
                    Err(err) => eprintln!("[yolo] inference error: {err}"),
                }
            }
        });
    });
}

// ---------------------------------------------------------------------------
// HTTP / WS handlers
// ---------------------------------------------------------------------------

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

#[derive(Deserialize)]
#[serde(untagged)]
enum ClientMsg {
    Switch { switch_camera: String },
    Confidence { confidence: f32 },
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    {
        let mut cam = state.camera.lock().unwrap();
        if cam.pipeline.is_none() {
            let dev = cam.device.clone();
            cam.start(&dev, state.frames_tx.clone(), state.latest_tx.clone());
        }
    }

    let mut rx = state.frames_tx.subscribe();

    loop {
        tokio::select! {
            frame = rx.recv() => {
                match frame {
                    Ok(data) => {
                        let meta_json = state.meta.read().unwrap().json.clone();
                        if socket.send(Message::Text(meta_json.into())).await.is_err() {
                            break;
                        }
                        if socket.send(Message::Binary(data.into())).await.is_err() {
                            break;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => break,
                }
            }
            msg = socket.recv() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(cmd) = serde_json::from_str::<ClientMsg>(&text) {
                            match cmd {
                                ClientMsg::Switch { switch_camera } => {
                                    let mut cam = state.camera.lock().unwrap();
                                    cam.start(&switch_camera, state.frames_tx.clone(), state.latest_tx.clone());
                                }
                                ClientMsg::Confidence { confidence } => {
                                    let v = confidence.clamp(0.05, 0.95);
                                    *state.confidence.write().unwrap() = v;
                                    println!("[yolo] confidence -> {v:.2}");
                                }
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => {}
                }
            }
        }
    }

    drop(rx);
    if state.frames_tx.receiver_count() == 0 {
        let mut cam = state.camera.lock().unwrap();
        cam.stop();
    }
}

#[derive(serde::Serialize)]
struct CameraInfo {
    id: String,
    name: String,
}

async fn list_cameras() -> impl IntoResponse {
    let output = std::process::Command::new("v4l2-ctl")
        .arg("--list-devices")
        .output();

    match output {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            let cameras = parse_v4l2_devices(&stdout);
            (StatusCode::OK, Json(serde_json::to_value(&cameras).unwrap()))
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!([{ "id": "", "name": e.to_string() }])),
        ),
    }
}

fn parse_v4l2_devices(output: &str) -> Vec<CameraInfo> {
    let mut cameras = Vec::new();
    let mut current_name: Option<String> = None;
    for line in output.lines() {
        if !line.starts_with('\t') && !line.starts_with(' ') && line.contains(':') {
            current_name = Some(line.trim().trim_end_matches(':').to_string());
        } else if let Some(ref name) = current_name {
            let dev = line.trim();
            if dev.starts_with("/dev/video") {
                cameras.push(CameraInfo {
                    id: dev.to_string(),
                    name: name.clone(),
                });
            }
        }
    }
    cameras
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn env_truthy(name: &str) -> bool {
    matches!(
        std::env::var(name).unwrap_or_default().to_lowercase().as_str(),
        "true" | "1" | "yes"
    )
}

fn is_rpi() -> bool {
    let dev = std::env::var("WENDY_DEVICE_TYPE").unwrap_or_default();
    if dev.starts_with("raspberrypi") {
        return true;
    }
    if !dev.is_empty() {
        return false;
    }
    std::fs::read_to_string("/proc/device-tree/model")
        .map(|s| s.contains("Raspberry Pi"))
        .unwrap_or(false)
}

#[tokio::main]
async fn main() {
    gstreamer::init().expect("Failed to initialize GStreamer");

    let use_gpu = env_truthy("WENDY_HAS_GPU");
    let rpi = is_rpi();
    let use_passthrough = !use_gpu || rpi;

    println!(
        "[startup] platform={}, has_gpu={}, is_rpi={}, capture={}",
        std::env::var("WENDY_PLATFORM").unwrap_or_else(|_| "unknown".into()),
        use_gpu,
        rpi,
        if use_passthrough { "passthrough" } else { "decode-encode" },
    );

    let (frames_tx, _rx) = broadcast::channel::<Vec<u8>>(16);
    let (latest_tx, latest_rx) = watch::channel::<Option<Vec<u8>>>(None);

    let state = AppState {
        camera: Arc::new(Mutex::new(MJPEGCamera::new(use_passthrough))),
        frames_tx: frames_tx.clone(),
        latest_tx: latest_tx.clone(),
        meta: Arc::new(RwLock::new(MetaState::default())),
        confidence: Arc::new(RwLock::new(0.25)),
    };

    let engine = match YoloEngine::new(use_gpu) {
        Ok(e) => e,
        Err(err) => {
            eprintln!("[yolo] failed to load model: {err}");
            std::process::exit(1);
        }
    };

    spawn_inference_task(state.clone(), latest_rx, engine, use_gpu);
    spawn_watchdog(state.clone());

    let app = Router::new()
        .route("/", get(index))
        .nest_service("/assets", tower_http::services::ServeDir::new("./assets"))
        .route("/cameras", get(list_cameras))
        .route("/stream", get(ws_handler))
        .with_state(state);

    let addr = "0.0.0.0:{{.PORT}}";
    println!("Starting server on {addr}");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("Failed to bind");

    axum::serve(listener, app).await.expect("Server error");
}
