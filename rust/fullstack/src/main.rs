use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, State,
    },
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use gstreamer::prelude::*;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::PathBuf,
    process::Command,
    sync::{Arc, Mutex},
};
use tokio::sync::broadcast;
use tower_http::services::{ServeDir, ServeFile};

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------

fn init_db() -> Connection {
    let db_path = PathBuf::from("/data/cars.db");
    if let Some(parent) = db_path.parent() {
        fs::create_dir_all(parent).ok();
    }
    let conn = Connection::open(&db_path).expect("Failed to open database");
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            color TEXT NOT NULL,
            year INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )",
    )
    .expect("Failed to create table");
    conn
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Car {
    id: i64,
    make: String,
    model: String,
    color: String,
    year: i32,
    created_at: Option<String>,
    updated_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CarInput {
    make: String,
    model: String,
    color: String,
    year: i32,
}

#[derive(Debug, Serialize)]
struct DeviceInfo {
    id: String,
    name: String,
}

#[derive(Debug, Serialize)]
struct GpuInfo {
    available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    memory: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    driver: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<String>,
}

#[derive(Debug, Serialize)]
struct SystemInfo {
    hostname: String,
    platform: String,
    architecture: String,
    uptime: String,
    memory: serde_json::Value,
    disk: serde_json::Value,
    cpu: serde_json::Value,
}

#[derive(Deserialize)]
struct CameraSwitch {
    switch_camera: Option<String>,
}

#[derive(Deserialize)]
struct MicSwitch {
    switch_microphone: Option<String>,
}

// ---------------------------------------------------------------------------
// GStreamer capture singleton
// ---------------------------------------------------------------------------

struct GstCapture {
    pipeline: Option<gstreamer::Element>,
    current_device: Option<String>,
    tx: broadcast::Sender<Vec<u8>>,
    client_count: usize,
}

struct CameraSingleton(Mutex<GstCapture>);
struct AudioSingleton(Mutex<GstCapture>);

fn build_camera_pipelines(device: &Option<String>) -> Vec<String> {
    let appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false";
    let src = match device {
        Some(d) => format!("v4l2src device={d}"),
        None => "v4l2src".to_string(),
    };
    vec![
        format!("{src} ! image/jpeg ! {appsink}"),
        format!("{src} ! image/jpeg,width=640,height=480 ! {appsink}"),
        format!("{src} ! videoconvert ! jpegenc quality=70 ! {appsink}"),
    ]
}

fn build_audio_pipelines(device: &Option<String>) -> Vec<String> {
    let appsink = "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
    let pcm = "audio/x-raw,format=S16LE,channels=1,rate=16000";

    if let Some(d) = device {
        vec![format!(
            "alsasrc device=\"{d}\" ! audioconvert ! audioresample ! {pcm} ! {appsink}"
        )]
    } else {
        let mut pipes = Vec::new();
        for mic in list_alsa_devices("arecord") {
            pipes.push(format!(
                "alsasrc device=\"{}\" ! audioconvert ! audioresample ! {pcm} ! {appsink}",
                mic.id
            ));
        }
        pipes.push(format!(
            "alsasrc ! audioconvert ! audioresample ! {pcm} ! {appsink}"
        ));
        pipes
    }
}

fn try_start_pipeline(
    descriptions: Vec<String>,
    tx: broadcast::Sender<Vec<u8>>,
) -> Option<gstreamer::Element> {
    for desc in descriptions {
        match gstreamer::parse::launch(&desc) {
            Ok(pipeline) => {
                let ret = pipeline.set_state(gstreamer::State::Paused);
                if ret == Err(gstreamer::StateChangeError) {
                    pipeline.set_state(gstreamer::State::Null).ok();
                    continue;
                }
                // Wait for preroll
                match pipeline.state(gstreamer::ClockTime::from_seconds(5)) {
                    (Err(_), _, _) => {
                        pipeline.set_state(gstreamer::State::Null).ok();
                        continue;
                    }
                    _ => {}
                }

                // Attach appsink callback
                let sink = pipeline
                    .downcast_ref::<gstreamer::Bin>()
                    .and_then(|b| b.by_name("sink"));
                if let Some(sink_el) = sink {
                    let appsink = sink_el
                        .downcast::<gstreamer_app::AppSink>()
                        .expect("sink is not AppSink");
                    let tx_clone = tx.clone();
                    appsink.set_callbacks(
                        gstreamer_app::AppSinkCallbacks::builder()
                            .new_sample(move |sink| {
                                let sample = sink.pull_sample().map_err(|_| gstreamer::FlowError::Error)?;
                                let buffer = sample.buffer().ok_or(gstreamer::FlowError::Error)?;
                                let map = buffer.map_readable().map_err(|_| gstreamer::FlowError::Error)?;
                                let data = map.as_slice().to_vec();
                                // Ignore send errors (no receivers)
                                let _ = tx_clone.send(data);
                                Ok(gstreamer::FlowSuccess::Ok)
                            })
                            .build(),
                    );
                }

                pipeline.set_state(gstreamer::State::Playing).ok();
                eprintln!("Pipeline ready: {desc}");
                return Some(pipeline);
            }
            Err(_) => continue,
        }
    }
    None
}

fn ensure_pipeline(capture: &mut GstCapture, build_fn: fn(&Option<String>) -> Vec<String>) {
    if capture.pipeline.is_none() && capture.client_count > 0 {
        let pipes = build_fn(&capture.current_device);
        capture.pipeline = try_start_pipeline(pipes, capture.tx.clone());
    }
}

fn stop_pipeline(capture: &mut GstCapture) {
    if let Some(ref p) = capture.pipeline {
        p.set_state(gstreamer::State::Null).ok();
    }
    capture.pipeline = None;
}

fn switch_device(
    capture: &mut GstCapture,
    device: String,
    build_fn: fn(&Option<String>) -> Vec<String>,
) {
    stop_pipeline(capture);
    capture.current_device = Some(device);
    ensure_pipeline(capture, build_fn);
}

// ---------------------------------------------------------------------------
// Shared application state
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct AppState {
    db: Arc<Mutex<Connection>>,
    camera: Arc<CameraSingleton>,
    audio: Arc<AudioSingleton>,
}

// ---------------------------------------------------------------------------
// Car CRUD handlers
// ---------------------------------------------------------------------------

fn row_to_car(row: &rusqlite::Row) -> rusqlite::Result<Car> {
    Ok(Car {
        id: row.get(0)?,
        make: row.get(1)?,
        model: row.get(2)?,
        color: row.get(3)?,
        year: row.get(4)?,
        created_at: row.get(5)?,
        updated_at: row.get(6)?,
    })
}

async fn list_cars(State(state): State<AppState>) -> Json<Vec<Car>> {
    let db = state.db.lock().unwrap();
    let mut stmt = db.prepare("SELECT id, make, model, color, year, created_at, updated_at FROM cars ORDER BY id").unwrap();
    let cars: Vec<Car> = stmt.query_map([], row_to_car).unwrap().filter_map(|r| r.ok()).collect();
    Json(cars)
}

async fn create_car(
    State(state): State<AppState>,
    Json(input): Json<CarInput>,
) -> impl IntoResponse {
    let db = state.db.lock().unwrap();
    db.execute(
        "INSERT INTO cars (make, model, color, year) VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![input.make, input.model, input.color, input.year],
    )
    .unwrap();
    let id = db.last_insert_rowid();
    let car = db
        .query_row(
            "SELECT id, make, model, color, year, created_at, updated_at FROM cars WHERE id = ?1",
            [id],
            row_to_car,
        )
        .unwrap();
    (StatusCode::CREATED, Json(car))
}

async fn get_car(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> Result<Json<Car>, StatusCode> {
    let db = state.db.lock().unwrap();
    db.query_row(
        "SELECT id, make, model, color, year, created_at, updated_at FROM cars WHERE id = ?1",
        [id],
        row_to_car,
    )
    .map(Json)
    .map_err(|_| StatusCode::NOT_FOUND)
}

async fn update_car(
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(input): Json<CarInput>,
) -> Result<Json<Car>, StatusCode> {
    let db = state.db.lock().unwrap();
    let affected = db
        .execute(
            "UPDATE cars SET make=?1, model=?2, color=?3, year=?4, updated_at=datetime('now') WHERE id=?5",
            rusqlite::params![input.make, input.model, input.color, input.year, id],
        )
        .unwrap();
    if affected == 0 {
        return Err(StatusCode::NOT_FOUND);
    }
    db.query_row(
        "SELECT id, make, model, color, year, created_at, updated_at FROM cars WHERE id = ?1",
        [id],
        row_to_car,
    )
    .map(Json)
    .map_err(|_| StatusCode::NOT_FOUND)
}

async fn delete_car(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> StatusCode {
    let db = state.db.lock().unwrap();
    let affected = db
        .execute("DELETE FROM cars WHERE id = ?1", [id])
        .unwrap();
    if affected > 0 {
        StatusCode::NO_CONTENT
    } else {
        StatusCode::NOT_FOUND
    }
}

// ---------------------------------------------------------------------------
// Device listing helpers
// ---------------------------------------------------------------------------

fn list_cameras() -> Vec<DeviceInfo> {
    let mut cameras = Vec::new();
    let Ok(entries) = fs::read_dir("/dev") else {
        return cameras;
    };
    let mut video_paths: Vec<PathBuf> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("video"))
                .unwrap_or(false)
        })
        .collect();
    video_paths.sort();

    for path in video_paths {
        let path_str = path.to_string_lossy().to_string();
        // Check if it is a capture device
        if let Ok(output) = Command::new("v4l2-ctl")
            .args(["--device", &path_str, "--all"])
            .output()
        {
            let text = String::from_utf8_lossy(&output.stdout);
            if !text.contains("Video Capture") {
                continue;
            }
        } else {
            continue;
        }
        // Get card name
        let name = Command::new("v4l2-ctl")
            .args(["--device", &path_str, "--info"])
            .output()
            .ok()
            .and_then(|o| {
                let text = String::from_utf8_lossy(&o.stdout).to_string();
                text.lines()
                    .find(|l| l.contains("Card type"))
                    .and_then(|l| l.split_once(':'))
                    .map(|(_, v)| v.trim().to_string())
            })
            .unwrap_or_else(|| {
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string()
            });
        cameras.push(DeviceInfo { id: path_str, name });
    }
    cameras
}

fn list_alsa_devices(cmd: &str) -> Vec<DeviceInfo> {
    let mut devs = Vec::new();
    let Ok(output) = Command::new(cmd).arg("-l").output() else {
        return devs;
    };
    let text = String::from_utf8_lossy(&output.stdout);
    for line in text.lines() {
        if line.starts_with("card ") {
            let parts: Vec<&str> = line.splitn(3, ':').collect();
            if parts.len() >= 2 {
                let card = line
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("0")
                    .trim_end_matches(':');
                let name = parts[1]
                    .trim()
                    .split('[')
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_string();
                devs.push(DeviceInfo {
                    id: format!("hw:{card},0"),
                    name,
                });
            }
        }
    }
    devs
}

// ---------------------------------------------------------------------------
// Device listing routes
// ---------------------------------------------------------------------------

async fn get_cameras() -> Json<Vec<DeviceInfo>> {
    Json(list_cameras())
}

async fn get_microphones() -> Json<Vec<DeviceInfo>> {
    Json(list_alsa_devices("arecord"))
}

async fn get_speakers() -> Json<Vec<DeviceInfo>> {
    Json(list_alsa_devices("aplay"))
}

// ---------------------------------------------------------------------------
// GPU info
// ---------------------------------------------------------------------------

async fn gpu_info() -> Json<GpuInfo> {
    // Try nvidia-smi first
    if let Ok(output) = Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total,driver_version,temperature.gpu",
            "--format=csv,noheader,nounits",
        ])
        .output()
    {
        let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if output.status.success() && !text.is_empty() {
            let parts: Vec<&str> = text.split(',').map(|s| s.trim()).collect();
            return Json(GpuInfo {
                available: true,
                name: parts.first().map(|s| s.to_string()),
                memory: parts.get(1).map(|s| format!("{s} MiB")),
                driver: parts.get(2).map(|s| s.to_string()),
                temperature: parts.get(3).map(|s| format!("{s}\u{00B0}C")),
            });
        }
    }

    // Fallback: /sys/class/thermal
    if let Ok(temp_str) = fs::read_to_string("/sys/class/thermal/thermal_zone0/temp") {
        if let Ok(millideg) = temp_str.trim().parse::<f64>() {
            return Json(GpuInfo {
                available: true,
                name: Some("ARM GPU".to_string()),
                memory: None,
                driver: None,
                temperature: Some(format!("{:.1}\u{00B0}C", millideg / 1000.0)),
            });
        }
    }

    Json(GpuInfo {
        available: false,
        name: None,
        memory: None,
        driver: None,
        temperature: None,
    })
}

// ---------------------------------------------------------------------------
// System info
// ---------------------------------------------------------------------------

async fn system_info() -> Json<SystemInfo> {
    let hostname = std::env::var("WENDY_HOSTNAME").unwrap_or_else(|_| {
        fs::read_to_string("/etc/hostname")
            .unwrap_or_else(|_| "unknown".to_string())
            .trim()
            .to_string()
    });

    // Memory from /proc/meminfo
    let memory = {
        let mut mem = serde_json::Map::new();
        if let Ok(content) = fs::read_to_string("/proc/meminfo") {
            let mut total_kb: Option<u64> = None;
            let mut avail_kb: Option<u64> = None;
            for line in content.lines() {
                if line.starts_with("MemTotal") {
                    if let Some(val) = line.split_whitespace().nth(1).and_then(|v| v.parse().ok()) {
                        total_kb = Some(val);
                        mem.insert("total".into(), format!("{} MB", val / 1024).into());
                    }
                } else if line.starts_with("MemAvailable") {
                    if let Some(val) = line.split_whitespace().nth(1).and_then(|v| v.parse().ok()) {
                        avail_kb = Some(val);
                        mem.insert("free".into(), format!("{} MB", val / 1024).into());
                    }
                }
            }
            if let (Some(t), Some(a)) = (total_kb, avail_kb) {
                mem.insert("used".into(), format!("{} MB", (t - a) / 1024).into());
            }
        }
        serde_json::Value::Object(mem)
    };

    // Disk usage via statvfs
    let disk = {
        let mut d = serde_json::Map::new();
        // Use df as a portable approach
        if let Ok(output) = Command::new("df")
            .args(["--output=size,used,avail", "-B1", "/"])
            .output()
        {
            let text = String::from_utf8_lossy(&output.stdout);
            if let Some(line) = text.lines().nth(1) {
                let vals: Vec<u64> = line
                    .split_whitespace()
                    .filter_map(|v| v.parse().ok())
                    .collect();
                if vals.len() >= 3 {
                    d.insert("total".into(), format!("{} GB", vals[0] / (1024 * 1024 * 1024)).into());
                    d.insert("used".into(), format!("{} GB", vals[1] / (1024 * 1024 * 1024)).into());
                    d.insert("free".into(), format!("{} GB", vals[2] / (1024 * 1024 * 1024)).into());
                }
            }
        }
        serde_json::Value::Object(d)
    };

    // CPU info
    let cpu = {
        let mut c = serde_json::Map::new();
        let cores = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(0);
        c.insert("cores".into(), cores.into());
        if let Ok(content) = fs::read_to_string("/proc/cpuinfo") {
            let model = content
                .lines()
                .find(|l| l.starts_with("model name"))
                .and_then(|l| l.split_once(':'))
                .map(|(_, v)| v.trim().to_string());
            if let Some(m) = model {
                c.insert("model".into(), m.into());
            }
        }
        if !c.contains_key("model") {
            c.insert("model".into(), std::env::consts::ARCH.into());
        }
        serde_json::Value::Object(c)
    };

    // Uptime
    let uptime = fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|s| s.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()))
        .map(|secs| {
            let h = (secs / 3600.0) as u64;
            let m = ((secs % 3600.0) / 60.0) as u64;
            format!("{h}h {m}m")
        })
        .unwrap_or_default();

    Json(SystemInfo {
        hostname,
        platform: std::env::consts::OS.to_string(),
        architecture: std::env::consts::ARCH.to_string(),
        uptime,
        memory,
        disk,
        cpu,
    })
}

// ---------------------------------------------------------------------------
// WebSocket camera stream
// ---------------------------------------------------------------------------

async fn camera_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| camera_stream(socket, state))
}

async fn camera_stream(socket: WebSocket, state: AppState) {
    let (mut ws_tx, mut ws_rx) = socket.split();
    use futures_util::{SinkExt, StreamExt};

    // Subscribe before incrementing to avoid missing frames
    let mut rx = {
        let mut cap = state.camera.0.lock().unwrap();
        let rx = cap.tx.subscribe();
        cap.client_count += 1;
        ensure_pipeline(&mut cap, build_camera_pipelines);
        rx
    };

    // Send frames to the client
    let camera_ref = state.camera.clone();
    let send_task = tokio::spawn(async move {
        while let Ok(data) = rx.recv().await {
            if ws_tx.send(Message::Binary(data.into())).await.is_err() {
                break;
            }
        }
    });

    // Receive switch commands
    let camera_ref2 = state.camera.clone();
    let recv_task = tokio::spawn(async move {
        while let Some(Ok(msg)) = ws_rx.next().await {
            if let Message::Text(text) = msg {
                if let Ok(cmd) = serde_json::from_str::<CameraSwitch>(&text) {
                    if let Some(dev) = cmd.switch_camera {
                        let mut cap = camera_ref2.0.lock().unwrap();
                        switch_device(&mut cap, dev, build_camera_pipelines);
                    }
                }
            }
        }
    });

    tokio::select! {
        _ = send_task => {},
        _ = recv_task => {},
    }

    let mut cap = camera_ref.0.lock().unwrap();
    cap.client_count -= 1;
    if cap.client_count == 0 {
        stop_pipeline(&mut cap);
    }
}

// ---------------------------------------------------------------------------
// WebSocket audio stream
// ---------------------------------------------------------------------------

async fn audio_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| audio_stream(socket, state))
}

async fn audio_stream(socket: WebSocket, state: AppState) {
    let (mut ws_tx, mut ws_rx) = socket.split();
    use futures_util::{SinkExt, StreamExt};

    let mut rx = {
        let mut cap = state.audio.0.lock().unwrap();
        let rx = cap.tx.subscribe();
        cap.client_count += 1;
        ensure_pipeline(&mut cap, build_audio_pipelines);
        rx
    };

    let audio_ref = state.audio.clone();
    let send_task = tokio::spawn(async move {
        while let Ok(data) = rx.recv().await {
            if ws_tx.send(Message::Binary(data.into())).await.is_err() {
                break;
            }
        }
    });

    let audio_ref2 = state.audio.clone();
    let recv_task = tokio::spawn(async move {
        while let Some(Ok(msg)) = ws_rx.next().await {
            if let Message::Text(text) = msg {
                if let Ok(cmd) = serde_json::from_str::<MicSwitch>(&text) {
                    if let Some(dev) = cmd.switch_microphone {
                        let mut cap = audio_ref2.0.lock().unwrap();
                        switch_device(&mut cap, dev, build_audio_pipelines);
                    }
                }
            }
        }
    });

    tokio::select! {
        _ = send_task => {},
        _ = recv_task => {},
    }

    let mut cap = audio_ref.0.lock().unwrap();
    cap.client_count -= 1;
    if cap.client_count == 0 {
        stop_pipeline(&mut cap);
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    gstreamer::init().expect("Failed to initialise GStreamer");

    let hostname = std::env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "unknown".to_string());

    let (cam_tx, _) = broadcast::channel::<Vec<u8>>(16);
    let (aud_tx, _) = broadcast::channel::<Vec<u8>>(16);

    let state = AppState {
        db: Arc::new(Mutex::new(init_db())),
        camera: Arc::new(CameraSingleton(Mutex::new(GstCapture {
            pipeline: None,
            current_device: None,
            tx: cam_tx,
            client_count: 0,
        }))),
        audio: Arc::new(AudioSingleton(Mutex::new(GstCapture {
            pipeline: None,
            current_device: None,
            tx: aud_tx,
            client_count: 0,
        }))),
    };

    let api_routes = Router::new()
        .route("/api/cars", get(list_cars).post(create_car))
        .route(
            "/api/cars/{id}",
            get(get_car).put(update_car).delete(delete_car),
        )
        .route("/api/cameras", get(get_cameras))
        .route("/api/microphones", get(get_microphones))
        .route("/api/speakers", get(get_speakers))
        .route("/api/gpu", get(gpu_info))
        .route("/api/system", get(system_info))
        .route("/api/camera/stream", get(camera_ws_handler))
        .route("/api/audio/stream", get(audio_ws_handler))
        .with_state(state);

    let serve_dir = ServeDir::new("./static").fallback(ServeFile::new("./static/index.html"));

    let app = api_routes.fallback_service(serve_dir);

    let addr = "0.0.0.0:{{.PORT}}";
    eprintln!("Starting server on {addr} (hostname: {hostname})");

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
