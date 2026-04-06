use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::{header, StatusCode},
    response::{Html, IntoResponse, Json},
    routing::get,
    Router,
};
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use serde::Deserialize;
use std::sync::{Arc, Mutex};
use tokio::sync::broadcast;

const INDEX_HTML: &str = include_str!("../index.html");
const WENDY_LOGO: &str = include_str!("../assets/wendy-logo.svg");

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
    fn start(&mut self, device: &str) {
        // Tear down any existing pipeline first.
        self.stop();
        self.device = device.to_string();

        let launch = format!(
            "v4l2src device={device} ! image/jpeg ! jpegdec ! jpegenc quality=85 ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        );

        let element = gstreamer::parse::launch(&launch).expect("Failed to create pipeline");
        let pipeline = element
            .dynamic_cast::<gstreamer::Pipeline>()
            .expect("Not a pipeline");

        let sink = pipeline
            .by_name("sink")
            .expect("Sink element not found")
            .dynamic_cast::<AppSink>()
            .expect("Not an AppSink");

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

        pipeline
            .set_state(gstreamer::State::Playing)
            .expect("Failed to start pipeline");

        self.pipeline = Some(pipeline);
    }

    /// Stop the current pipeline.
    fn stop(&mut self) {
        if let Some(ref pipeline) = self.pipeline.take() {
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

async fn wendy_logo() -> impl IntoResponse {
    ([(header::CONTENT_TYPE, "image/svg+xml")], WENDY_LOGO)
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
    {
        let mut cam = state.camera.lock().unwrap();
        if cam.pipeline.is_none() {
            let dev = cam.device.clone();
            cam.start(&dev);
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
                            cam.start(&cmd.switch_camera);
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

/// Parse `v4l2-ctl --list-devices` output into a vec of CameraInfo.
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

#[tokio::main]
async fn main() {
    gstreamer::init().expect("Failed to initialize GStreamer");

    let hostname = std::env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "unknown".to_string());

    let (tx, _rx) = broadcast::channel::<Vec<u8>>(16);

    let camera = Arc::new(Mutex::new(MJPEGCamera::new(tx.clone())));

    let state = AppState {
        camera,
        tx,
    };

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
