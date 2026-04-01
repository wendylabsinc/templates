use axum::{
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::env;

#[tokio::main]
async fn main() {
    let hostname = env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "0.0.0.0".to_string());

    let app = Router::new()
        .route("/", get(root))
        .route("/health", get(health))
        .route("/items", post(create_item));

    let listener = tokio::net::TcpListener::bind("0.0.0.0:{{PORT}}").await.unwrap();
    println!("Server running on http://{}:{{PORT}}", hostname);
    axum::serve(listener, app).await.unwrap();
}

async fn root() -> Json<serde_json::Value> {
    println!("Received request: GET /");
    Json(serde_json::json!({"message": "hello-world"}))
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status": "ok"}))
}

async fn create_item(Json(payload): Json<CreateItem>) -> (StatusCode, Json<ItemResponse>) {
    println!("Received request: POST /items - {}", payload.name);
    let item = ItemResponse {
        id: 1,
        name: payload.name,
        price: payload.price,
    };
    (StatusCode::CREATED, Json(item))
}

#[derive(Deserialize)]
struct CreateItem {
    name: String,
    price: f64,
}

#[derive(Serialize)]
struct ItemResponse {
    id: u64,
    name: String,
    price: f64,
}
