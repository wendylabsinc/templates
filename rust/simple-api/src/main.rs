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

    let listener = bind_listener({{.PORT}}).unwrap();
    println!("Server running on http://{}:{{.PORT}}", hostname);
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

// Keep IPv4 LAN and IPv6 USB connections on the same listener.
fn bind_listener(port: u16) -> std::io::Result<tokio::net::TcpListener> {
    let socket = socket2::Socket::new(socket2::Domain::IPV6, socket2::Type::STREAM, Some(socket2::Protocol::TCP))?;
    socket.set_only_v6(false)?;
    socket.set_reuse_address(true)?;
    socket.set_nonblocking(true)?;
    socket.bind(&std::net::SocketAddr::from(([0u16; 8], port)).into())?;
    socket.listen(1024)?;
    tokio::net::TcpListener::from_std(socket.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn listener_accepts_ipv4_and_ipv6() {
        let listener = bind_listener(0).unwrap();
        let port = listener.local_addr().unwrap().port();
        for host in ["127.0.0.1", "::1"] {
            let client = tokio::net::TcpStream::connect((host, port)).await.unwrap();
            let (_, peer) = listener.accept().await.unwrap();
            assert_eq!(peer.port(), client.local_addr().unwrap().port());
        }
    }
}
