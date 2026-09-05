use axum::{routing::get, Json, Router};
use serde_json::{json, Value};
use tower_http::services::ServeDir;

async fn hello() -> Json<Value> {
    Json(json!({"message": "Hello from Wendy!"}))
}

#[tokio::main]
async fn main() {
    let app = Router::new()
        .route("/api/hello", get(hello))
        .route("/health", get(|| async { Json(json!({"status": "ok"})) }))
        .fallback_service(ServeDir::new("static"));
    let listener = tokio::net::TcpListener::bind("0.0.0.0:{{.PORT}}")
        .await.expect("Could not bind HTTP port");
    axum::serve(listener, app).await.expect("HTTP server failed");
}
