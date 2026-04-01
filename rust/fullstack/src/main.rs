use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::{atomic::AtomicU64, Arc, Mutex};
use tower_http::services::{ServeDir, ServeFile};

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Car {
    id: u64,
    make: String,
    model: String,
    color: String,
    year: i32,
}

#[derive(Debug, Deserialize)]
struct CreateCar {
    make: String,
    model: String,
    color: String,
    year: i32,
}

#[derive(Clone)]
struct AppState {
    cars: Arc<Mutex<Vec<Car>>>,
    next_id: Arc<AtomicU64>,
}

#[tokio::main]
async fn main() {
    let hostname = std::env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "unknown".to_string());

    let state = AppState {
        cars: Arc::new(Mutex::new(Vec::new())),
        next_id: Arc::new(AtomicU64::new(1)),
    };

    let api_routes = Router::new()
        .route("/api/cars", get(list_cars).post(create_car))
        .route(
            "/api/cars/{id}",
            get(get_car).put(update_car).delete(delete_car),
        )
        .with_state(state);

    let serve_dir = ServeDir::new("./static").fallback(ServeFile::new("./static/index.html"));

    let app = api_routes.fallback_service(serve_dir);

    let addr = "0.0.0.0:{{.PORT}}";
    println!("Starting server on {addr} (hostname: {hostname})");

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn list_cars(State(state): State<AppState>) -> Json<Vec<Car>> {
    let cars = state.cars.lock().unwrap();
    Json(cars.clone())
}

async fn create_car(
    State(state): State<AppState>,
    Json(input): Json<CreateCar>,
) -> impl IntoResponse {
    let id = state
        .next_id
        .fetch_add(1, std::sync::atomic::Ordering::SeqCst);

    let car = Car {
        id,
        make: input.make,
        model: input.model,
        color: input.color,
        year: input.year,
    };

    state.cars.lock().unwrap().push(car.clone());
    (StatusCode::CREATED, Json(car))
}

async fn get_car(
    State(state): State<AppState>,
    Path(id): Path<u64>,
) -> Result<Json<Car>, StatusCode> {
    let cars = state.cars.lock().unwrap();
    cars.iter()
        .find(|c| c.id == id)
        .cloned()
        .map(Json)
        .ok_or(StatusCode::NOT_FOUND)
}

async fn update_car(
    State(state): State<AppState>,
    Path(id): Path<u64>,
    Json(input): Json<CreateCar>,
) -> Result<Json<Car>, StatusCode> {
    let mut cars = state.cars.lock().unwrap();
    let car = cars.iter_mut().find(|c| c.id == id).ok_or(StatusCode::NOT_FOUND)?;

    car.make = input.make;
    car.model = input.model;
    car.color = input.color;
    car.year = input.year;

    Ok(Json(car.clone()))
}

async fn delete_car(
    State(state): State<AppState>,
    Path(id): Path<u64>,
) -> StatusCode {
    let mut cars = state.cars.lock().unwrap();
    let len_before = cars.len();
    cars.retain(|c| c.id != id);

    if cars.len() < len_before {
        StatusCode::NO_CONTENT
    } else {
        StatusCode::NOT_FOUND
    }
}
