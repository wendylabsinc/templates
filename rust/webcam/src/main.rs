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
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::{Arc, Mutex};
use tokio::sync::broadcast;

const INDEX_HTML: &str = include_str!("../index.html");

// ---------------------------------------------------------------------------
// V4L2 helpers — device capability / naming lookups via v4l2-ctl.
// Mirrors python/webcam/app.py's _v4l2_is_capture / _v4l2_device_name.
// ---------------------------------------------------------------------------

/// Returns true if the given `/dev/videoN` node is a capture-capable device
/// (as opposed to a metadata-only or output-only node some UVC cameras
/// expose alongside their capture node).
fn v4l2_is_capture(path: &str) -> bool {
    std::process::Command::new("v4l2-ctl")
        .args(["--device", path, "--all"])
        .output()
        .map(|out| String::from_utf8_lossy(&out.stdout).contains("Video Capture"))
        .unwrap_or(false)
}

/// Reads the human-readable "Card type" for a `/dev/videoN` node, falling
/// back to the device basename if `v4l2-ctl` is unavailable or the field is
/// missing.
fn v4l2_device_name(path: &str) -> String {
    let output = std::process::Command::new("v4l2-ctl")
        .args(["--device", path, "--info"])
        .output();

    if let Ok(out) = output {
        let stdout = String::from_utf8_lossy(&out.stdout);
        for line in stdout.lines() {
            if let Some((_, rest)) = line.split_once("Card type") {
                if let Some((_, name)) = rest.split_once(':') {
                    return name.trim().to_string();
                }
            }
        }
    }

    Path::new(path)
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string())
}

/// Fallback enumeration used when `v4l2-ctl --list-devices` yields nothing
/// (e.g. udev naming quirks). Scans `/dev/video*` directly, mirroring
/// python/webcam/app.py's `enumerate_cameras()` fallback branch.
fn enumerate_cameras_fallback() -> Vec<CameraInfo> {
    let mut paths: Vec<String> = std::fs::read_dir("/dev")
        .map(|entries| {
            entries
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter_map(|p| p.to_str().map(str::to_string))
                .filter(|p| p.starts_with("/dev/video"))
                .collect()
        })
        .unwrap_or_default();
    paths.sort();

    paths
        .into_iter()
        .filter(|p| v4l2_is_capture(p))
        .map(|p| {
            let name = v4l2_device_name(&p);
            CameraInfo { id: p, name }
        })
        .collect()
}

/// Enumerate capture-capable V4L2 devices as `[{"id", "name"}, ...]`.
/// Mirrors python/webcam/app.py's `enumerate_cameras()`: group by
/// `v4l2-ctl --list-devices`, keep only capture-capable nodes, and fall
/// back to scanning `/dev/video*` directly if that yields nothing.
fn enumerate_cameras() -> Vec<CameraInfo> {
    let output = std::process::Command::new("v4l2-ctl")
        .arg("--list-devices")
        .output();

    let mut cameras = match output {
        Ok(out) => parse_v4l2_devices(&String::from_utf8_lossy(&out.stdout)),
        Err(_) => Vec::new(),
    };

    if cameras.is_empty() {
        cameras = enumerate_cameras_fallback();
    }

    cameras
}

/// Parse `v4l2-ctl --list-devices` output into a vec of `CameraInfo`,
/// keeping only capture-capable device nodes.
///
/// Example input:
/// ```text
/// HD Webcam (usb-0000:00:14.0-1):
///     /dev/video0
///     /dev/video1
/// ```
fn parse_v4l2_devices(output: &str) -> Vec<CameraInfo> {
    let mut cameras = Vec::new();
    let mut current_name: Option<String> = None;

    for line in output.lines() {
        if !line.starts_with('\t') && !line.starts_with(' ') && line.contains(':') {
            // This is a device header line — strip the trailing colon.
            current_name = Some(line.trim().trim_end_matches(':').to_string());
        } else if let Some(ref name) = current_name {
            let dev = line.trim();
            if dev.starts_with("/dev/video") && v4l2_is_capture(dev) {
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
// MJPEGCamera — singleton that owns the GStreamer pipeline
// ---------------------------------------------------------------------------

struct MJPEGCamera {
    tx: broadcast::Sender<Vec<u8>>,
    pipeline: Option<gstreamer::Pipeline>,
    device: String,
}

impl MJPEGCamera {
    fn new(tx: broadcast::Sender<Vec<u8>>) -> Self {
        Self {
            tx,
            pipeline: None,
            device: "/dev/video0".to_string(),
        }
    }

    /// Start (or restart) the GStreamer pipeline for the given device.
    ///
    /// Tries a ladder of pipeline descriptions from most to least permissive,
    /// mirroring python/webcam/app.py's `MJPEGCamera._start_pipeline`: native
    /// MJPEG first (no transcoding needed on most USB webcams), then a
    /// constrained-resolution MJPEG caps, then a software JPEG re-encode.
    /// Each candidate is prerolled (PAUSED) so caps negotiation failures are
    /// caught before committing to it. Returns `true` on success.
    fn start(&mut self, device: &str) -> bool {
        self.stop();
        self.device = device.to_string();

        let src = format!("v4l2src device={device}");
        let appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false";
        let candidates = [
            format!("{src} ! image/jpeg ! {appsink}"),
            format!("{src} ! image/jpeg,width=640,height=480 ! {appsink}"),
            format!("{src} ! videoconvert ! jpegenc quality=70 ! {appsink}"),
        ];

        for desc in &candidates {
            let Some(pipeline) = Self::try_pipeline(desc) else {
                continue;
            };

            let sink = pipeline
                .by_name("sink")
                .and_then(|e| e.dynamic_cast::<AppSink>().ok());
            let Some(sink) = sink else {
                let _ = pipeline.set_state(gstreamer::State::Null);
                continue;
            };

            let tx = self.tx.clone();
            sink.set_callbacks(
                gstreamer_app::AppSinkCallbacks::builder()
                    .new_sample(move |appsink| {
                        let sample = appsink.pull_sample().map_err(|_| gstreamer::FlowError::Eos)?;
                        if let Some(buffer) = sample.buffer() {
                            let map = buffer.map_readable().map_err(|_| gstreamer::FlowError::Error)?;
                            let _ = tx.send(map.as_slice().to_vec());
                        }
                        Ok(gstreamer::FlowSuccess::Ok)
                    })
                    .build(),
            );

            if pipeline.set_state(gstreamer::State::Playing).is_err() {
                let _ = pipeline.set_state(gstreamer::State::Null);
                println!("Pipeline failed to reach PLAYING: {desc}");
                continue;
            }

            println!("Pipeline ready on {device}: {desc}");
            self.pipeline = Some(pipeline);
            return true;
        }

        println!("No working GStreamer pipeline found for device {device}");
        false
    }

    /// Parse and preroll a single pipeline candidate, returning it only if
    /// it successfully reaches PAUSED.
    fn try_pipeline(desc: &str) -> Option<gstreamer::Pipeline> {
        let element = match gstreamer::parse::launch(desc) {
            Ok(e) => e,
            Err(e) => {
                println!("Pipeline exception: {desc} — {e}");
                return None;
            }
        };
        let pipeline = element.dynamic_cast::<gstreamer::Pipeline>().ok()?;

        match pipeline.set_state(gstreamer::State::Paused) {
            Ok(gstreamer::StateChangeSuccess::Async) => {
                let (result, _, _) = pipeline.state(gstreamer::ClockTime::from_seconds(5));
                if result.is_err() {
                    let _ = pipeline.set_state(gstreamer::State::Null);
                    println!("Pipeline preroll failed: {desc}");
                    return None;
                }
            }
            Err(_) => {
                let _ = pipeline.set_state(gstreamer::State::Null);
                println!("Pipeline failed: {desc}");
                return None;
            }
            _ => {}
        }

        Some(pipeline)
    }

    /// Stop the current pipeline.
    fn stop(&mut self) {
        if let Some(pipeline) = self.pipeline.take() {
            let _ = pipeline.set_state(gstreamer::State::Null);
        }
    }
}

// ---------------------------------------------------------------------------
// Shared application state
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct AppState {
    camera: Arc<Mutex<MJPEGCamera>>,
    tx: broadcast::Sender<Vec<u8>>,
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

#[derive(Deserialize)]
struct SwitchCamera {
    switch_camera: String,
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    // Ensure the pipeline is running when the first subscriber connects.
    // Dropping `socket` here closes the connection if no pipeline could be
    // started for the default device.
    {
        let mut cam = state.camera.lock().unwrap();
        if cam.pipeline.is_none() {
            let dev = cam.device.clone();
            if !cam.start(&dev) {
                return;
            }
        }
    }

    let mut rx = state.tx.subscribe();

    loop {
        tokio::select! {
            frame = rx.recv() => {
                match frame {
                    Ok(data) => {
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
                        if let Ok(cmd) = serde_json::from_str::<SwitchCamera>(&text) {
                            let mut cam = state.camera.lock().unwrap();
                            if !cam.start(&cmd.switch_camera) {
                                println!("Camera switch failed: {}", cmd.switch_camera);
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => {}
                }
            }
        }
    }

    // If no more receivers, stop the pipeline to free the camera.
    if state.tx.receiver_count() == 0 {
        let mut cam = state.camera.lock().unwrap();
        cam.stop();
    }
}

#[derive(Serialize)]
struct CameraInfo {
    id: String,
    name: String,
}

async fn list_cameras() -> impl IntoResponse {
    (StatusCode::OK, Json(enumerate_cameras()))
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    gstreamer::init().expect("Failed to initialize GStreamer");

    let hostname = std::env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "unknown".to_string());

    let (tx, _rx) = broadcast::channel::<Vec<u8>>(16);

    let camera = Arc::new(Mutex::new(MJPEGCamera::new(tx.clone())));

    let state = AppState { camera, tx };

    let app = Router::new()
        .route("/", get(index))
        .nest_service("/assets", tower_http::services::ServeDir::new("./assets"))
        .route("/cameras", get(list_cameras))
        .route("/stream", get(ws_handler))
        .with_state(state);

    let addr = "0.0.0.0:{{.PORT}}";
    println!("Starting server on {addr} (hostname: {hostname})");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("Failed to bind");

    axum::serve(listener, app).await.expect("Server error");
}
