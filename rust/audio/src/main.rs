use axum::{
    extract::ws::{Message, WebSocket, WebSocketUpgrade},
    response::{Html, IntoResponse, Json},
    routing::get,
    Router,
};
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use serde_json::json;
use std::sync::OnceLock;
use tokio::sync::broadcast;
use tower_http::services::ServeDir;

static AUDIO_TX: OnceLock<broadcast::Sender<Vec<u8>>> = OnceLock::new();

struct AudioCapture {
    _pipeline: gstreamer::Element,
}

impl AudioCapture {
    fn start() -> Self {
        let (tx, _) = broadcast::channel::<Vec<u8>>(16);
        AUDIO_TX.get_or_init(|| tx.clone());

        gstreamer::init().expect("Failed to initialize GStreamer");

        let pipeline = gstreamer::parse::launch(
            "autoaudiosrc ! audioconvert ! audio/x-raw,format=S16LE,channels=1,rate=16000 ! appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false",
        )
        .expect("Failed to create GStreamer pipeline");

        let sink = pipeline
            .clone()
            .dynamic_cast::<gstreamer::Bin>()
            .expect("Pipeline is not a bin")
            .by_name("sink")
            .expect("Sink element not found")
            .dynamic_cast::<AppSink>()
            .expect("Element is not an AppSink");

        let tx_clone = tx.clone();
        sink.set_callbacks(
            gstreamer_app::AppSinkCallbacks::builder()
                .new_sample(move |appsink| {
                    let sample = appsink.pull_sample().map_err(|_| gstreamer::FlowError::Eos)?;
                    let buffer = sample.buffer().ok_or(gstreamer::FlowError::Error)?;
                    let map = buffer
                        .map_readable()
                        .map_err(|_| gstreamer::FlowError::Error)?;
                    let _ = tx_clone.send(map.as_slice().to_vec());
                    Ok(gstreamer::FlowSuccess::Ok)
                })
                .build(),
        );

        pipeline
            .set_state(gstreamer::State::Playing)
            .expect("Failed to start pipeline");

        AudioCapture {
            _pipeline: pipeline,
        }
    }
}

async fn ws_handler(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(handle_ws)
}

async fn handle_ws(mut socket: WebSocket) {
    let tx = AUDIO_TX.get().expect("AudioCapture not initialized");
    let mut rx = tx.subscribe();

    while let Ok(data) = rx.recv().await {
        if socket.send(Message::Binary(data.into())).await.is_err() {
            break;
        }
    }
}

async fn list_sounds() -> impl IntoResponse {
    let mut sounds = Vec::new();
    if let Ok(entries) = std::fs::read_dir("./assets") {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("wav") {
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    sounds.push(name.to_string());
                }
            }
        }
    }
    sounds.sort();
    Json(json!({ "sounds": sounds }))
}

async fn index() -> Html<&'static str> {
    Html(include_str!("../index.html"))
}

#[tokio::main]
async fn main() {
    let _capture = AudioCapture::start();

    let app = Router::new()
        .route("/", get(index))
        .route("/stream", get(ws_handler))
        .route("/sounds", get(list_sounds))
        .nest_service("/assets", ServeDir::new("./assets"));

    let addr = format!("0.0.0.0:{}", {{.PORT}});
    println!("Listening on {addr}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
