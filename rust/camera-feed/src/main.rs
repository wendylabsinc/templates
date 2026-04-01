use axum::{
    extract::ws::{Message, WebSocket, WebSocketUpgrade},
    response::{Html, IntoResponse, Json},
    routing::get,
    Router,
};
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use std::process::Command;
use tokio::sync::broadcast;

const INDEX_HTML: &str = include_str!("../index.html");

#[tokio::main]
async fn main() {
    gstreamer::init().expect("Failed to initialize GStreamer");

    let (tx, _rx) = broadcast::channel::<Vec<u8>>(16);

    // Spawn GStreamer capture task
    let frame_tx = tx.clone();
    tokio::spawn(async move {
        capture_frames(frame_tx).await;
    });

    let app = Router::new()
        .route("/", get(index))
        .route("/stream", get(move |ws| ws_handler(ws, tx)))
        .route("/cameras", get(list_cameras));

    let addr = "0.0.0.0:{{.PORT}}";
    println!("Listening on {addr}");

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("Failed to bind");

    axum::serve(listener, app).await.expect("Server error");
}

async fn index() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn ws_handler(ws: WebSocketUpgrade, tx: broadcast::Sender<Vec<u8>>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, tx))
}

async fn handle_socket(mut socket: WebSocket, tx: broadcast::Sender<Vec<u8>>) {
    let mut rx = tx.subscribe();

    while let Ok(frame) = rx.recv().await {
        if socket.send(Message::Binary(frame.into())).await.is_err() {
            break;
        }
    }
}

async fn capture_frames(tx: broadcast::Sender<Vec<u8>>) {
    let pipeline = gstreamer::parse::launch(
        "v4l2src device=/dev/video0 ! image/jpeg ! appsink name=sink",
    )
    .expect("Failed to create pipeline");

    let pipeline = pipeline
        .dynamic_cast::<gstreamer::Pipeline>()
        .expect("Not a pipeline");

    let sink = pipeline
        .by_name("sink")
        .expect("Sink element not found")
        .dynamic_cast::<AppSink>()
        .expect("Not an AppSink");

    sink.set_property("sync", false);

    pipeline
        .set_state(gstreamer::State::Playing)
        .expect("Failed to start pipeline");

    loop {
        match sink.pull_sample() {
            Ok(sample) => {
                if let Some(buffer) = sample.buffer() {
                    let map = buffer.map_readable().expect("Failed to map buffer");
                    let data = map.as_slice().to_vec();
                    let _ = tx.send(data);
                }
            }
            Err(_) => {
                eprintln!("Pipeline ended");
                break;
            }
        }
    }

    pipeline
        .set_state(gstreamer::State::Null)
        .expect("Failed to stop pipeline");
}

async fn list_cameras() -> impl IntoResponse {
    let output = Command::new("v4l2-ctl")
        .arg("--list-devices")
        .output();

    match output {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();

            Json(serde_json::json!({
                "devices": stdout.trim(),
                "error": if stderr.is_empty() { None } else { Some(stderr.trim().to_string()) }
            }))
        }
        Err(e) => Json(serde_json::json!({
            "devices": null,
            "error": e.to_string()
        })),
    }
}
