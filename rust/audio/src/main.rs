use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path,
    },
    http::StatusCode,
    response::{Html, IntoResponse, Json},
    routing::{get, post},
    Router,
};
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    process::{Command, Stdio},
    sync::{Mutex, OnceLock},
    thread,
};
use tokio::sync::{broadcast, mpsc};
use tower_http::services::ServeDir;

static AUDIO_TX: OnceLock<broadcast::Sender<Vec<u8>>> = OnceLock::new();
static MIC_SWITCH_TX: OnceLock<mpsc::UnboundedSender<String>> = OnceLock::new();
static SPEAKER: OnceLock<Mutex<Option<String>>> = OnceLock::new();

struct AudioCapture {
    pipeline: gstreamer::Element,
}

impl AudioCapture {
    fn start(device: Option<&str>) -> Self {
        let tx = AUDIO_TX
            .get_or_init(|| {
                let (tx, _) = broadcast::channel::<Vec<u8>>(16);
                tx
            })
            .clone();

        gstreamer::init().expect("Failed to initialize GStreamer");

        let source = match device {
            Some(device) => format!("alsasrc device=\"{}\"", escape_gst_value(device)),
            None => "autoaudiosrc".to_string(),
        };
        let pipeline_desc = format!(
            "{source} ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,channels=1,rate=16000 ! appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false"
        );

        let pipeline =
            gstreamer::parse::launch(&pipeline_desc).expect("Failed to create GStreamer pipeline");

        let sink = pipeline
            .clone()
            .dynamic_cast::<gstreamer::Bin>()
            .expect("Pipeline is not a bin")
            .by_name("sink")
            .expect("Sink element not found")
            .dynamic_cast::<AppSink>()
            .expect("Element is not an AppSink");

        sink.set_callbacks(
            gstreamer_app::AppSinkCallbacks::builder()
                .new_sample(move |appsink| {
                    let sample = appsink.pull_sample().map_err(|_| gstreamer::FlowError::Eos)?;
                    let buffer = sample.buffer().ok_or(gstreamer::FlowError::Error)?;
                    let map = buffer
                        .map_readable()
                        .map_err(|_| gstreamer::FlowError::Error)?;
                    let _ = tx.send(map.as_slice().to_vec());
                    Ok(gstreamer::FlowSuccess::Ok)
                })
                .build(),
        );

        pipeline
            .set_state(gstreamer::State::Playing)
            .expect("Failed to start pipeline");

        AudioCapture { pipeline }
    }
}

impl Drop for AudioCapture {
    fn drop(&mut self) {
        let _ = self.pipeline.set_state(gstreamer::State::Null);
    }
}

async fn ws_handler(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(handle_ws)
}

async fn handle_ws(mut socket: WebSocket) {
    let tx = AUDIO_TX.get().expect("AudioCapture not initialized");
    let mut rx = tx.subscribe();

    loop {
        tokio::select! {
            data = rx.recv() => match data {
                Ok(data) => {
                    if socket.send(Message::Binary(data.into())).await.is_err() {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => {}
                Err(broadcast::error::RecvError::Closed) => break,
            },
            message = socket.recv() => match message {
                Some(Ok(Message::Text(text))) => handle_ws_command(text.as_str()),
                Some(Ok(Message::Close(_))) | None => break,
                Some(Ok(_)) => {}
                Some(Err(_)) => break,
            },
        }
    }
}

fn handle_ws_command(text: &str) {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return;
    };

    if let Some(device) = value.get("switch_microphone").and_then(Value::as_str) {
        if let Some(tx) = MIC_SWITCH_TX.get() {
            let _ = tx.send(device.to_string());
        }
    }
}

async fn list_sounds() -> impl IntoResponse {
    let mut sounds = Vec::new();
    if let Ok(entries) = std::fs::read_dir("./assets") {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("wav") {
                if let Some(file) = path.file_name().and_then(|n| n.to_str()) {
                    sounds.push(json!({
                        "name": display_name(file),
                        "file": file,
                    }));
                }
            }
        }
    }
    sounds.sort_by_key(|sound| {
        sound
            .get("file")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    });
    Json(sounds)
}

async fn list_microphones() -> impl IntoResponse {
    Json(list_audio_devices("arecord"))
}

async fn list_speakers() -> impl IntoResponse {
    Json(list_audio_devices("aplay"))
}

async fn set_speaker(Path(device_id): Path<String>) -> impl IntoResponse {
    *speaker_state().lock().expect("speaker lock poisoned") = Some(device_id.clone());
    Json(json!({ "status": "ok", "speaker": device_id }))
}

async fn play_sound(Path(filename): Path<String>) -> impl IntoResponse {
    let Some(filepath) = resolve_sound_path(&filename) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "not found" })),
        )
            .into_response();
    };

    let speaker = speaker_state().lock().expect("speaker lock poisoned").clone();
    let mut args = vec![
        "filesrc".to_string(),
        format!("location={}", filepath.display()),
        "!".to_string(),
        "wavparse".to_string(),
        "!".to_string(),
        "audioconvert".to_string(),
        "!".to_string(),
        "audioresample".to_string(),
        "!".to_string(),
    ];

    if let Some(device) = speaker {
        args.push("alsasink".to_string());
        args.push(format!("device={device}"));
    } else {
        args.push("autoaudiosink".to_string());
    }

    match Command::new("gst-launch-1.0")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(mut child) => {
            thread::spawn(move || {
                let _ = child.wait();
            });
            (
                StatusCode::OK,
                Json(json!({ "status": "playing", "file": filename })),
            )
                .into_response()
        }
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": error.to_string() })),
        )
            .into_response(),
    }
}

async fn index() -> Html<&'static str> {
    Html(include_str!("../index.html"))
}

fn list_audio_devices(command: &str) -> Vec<Value> {
    let Ok(output) = Command::new(command)
        .arg("-l")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    else {
        return Vec::new();
    };

    let mut seen = std::collections::HashSet::new();
    let mut devices = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let Some(device) = parse_audio_device_line(line) else { continue };
        let id = device
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if seen.insert(id) {
            devices.push(device);
        }
    }
    devices
}

// `arecord -l` / `aplay -l` lines look like:
//   card 0: PCH [HDA Intel PCH], device 3: HDMI 0 [HDMI 0]
// HDMI outputs commonly use device 3, 7, etc., so we must capture the
// device number alongside the card number and dedupe on the pair.
fn parse_audio_device_line(line: &str) -> Option<Value> {
    if !line.starts_with("card ") {
        return None;
    }

    let (card_part, device_part) = line.split_once(", device ")?;

    let (card_prefix, card_rest) = card_part.split_once(':')?;
    let card_num = card_prefix.split_whitespace().nth(1)?.trim_end_matches(':');
    let card_name = card_rest
        .trim()
        .split('[')
        .next()
        .unwrap_or_default()
        .trim();

    let (device_num_part, device_rest) = device_part.split_once(':')?;
    let device_num = device_num_part.trim();
    let device_name = device_rest
        .trim()
        .split('[')
        .next()
        .unwrap_or_default()
        .trim();

    let name = match (card_name.is_empty(), device_name.is_empty()) {
        (false, false) => format!("{card_name} - {device_name}"),
        (false, true) => card_name.to_string(),
        (true, false) => device_name.to_string(),
        (true, true) => format!("Card {card_num} device {device_num}"),
    };

    Some(json!({
        "id": format!("hw:{card_num},{device_num}"),
        "name": name,
    }))
}

fn resolve_sound_path(filename: &str) -> Option<PathBuf> {
    if filename.contains('/') || filename.contains('\\') || !filename.to_lowercase().ends_with(".wav")
    {
        return None;
    }

    let filepath = PathBuf::from("./assets").join(filename);
    filepath.is_file().then_some(filepath)
}

fn speaker_state() -> &'static Mutex<Option<String>> {
    SPEAKER.get_or_init(|| Mutex::new(None))
}

fn display_name(file: &str) -> String {
    let stem = file.strip_suffix(".wav").unwrap_or(file);
    let mut capitalize_next = true;
    stem.chars()
        .map(|ch| {
            if ch == '-' || ch == '_' {
                capitalize_next = true;
                ' '
            } else if capitalize_next {
                capitalize_next = false;
                ch.to_ascii_uppercase()
            } else {
                ch
            }
        })
        .collect()
}

fn escape_gst_value(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

#[tokio::main]
async fn main() {
    let (switch_tx, mut switch_rx) = mpsc::unbounded_channel::<String>();
    let _ = MIC_SWITCH_TX.get_or_init(|| switch_tx);

    let capture = AudioCapture::start(None);
    tokio::spawn(async move {
        let mut capture = Some(capture);
        while let Some(device) = switch_rx.recv().await {
            capture.take();
            capture = Some(AudioCapture::start(Some(&device)));
        }
    });

    let app = Router::new()
        .route("/", get(index))
        .route("/stream", get(ws_handler))
        .route("/sounds", get(list_sounds))
        .route("/microphones", get(list_microphones))
        .route("/speakers", get(list_speakers))
        .route("/speaker/{device_id}", post(set_speaker))
        .route("/play/{filename}", post(play_sound))
        .nest_service("/assets", ServeDir::new("./assets"));

    let addr = format!("0.0.0.0:{}", {{.PORT}});
    println!("Listening on {addr}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
