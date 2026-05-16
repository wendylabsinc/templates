use anyhow::{anyhow, Context as AnyhowContext, Result};
use async_stream::stream;
use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use bytes::Bytes;
use jpeg_encoder::{ColorType, Encoder};
use realsense_rust::{
    config::Config,
    context::Context as RealSenseContext,
    frame::{ColorFrame, DepthFrame, InfraredFrame, PixelKind},
    kind::{Rs2Format, Rs2Option, Rs2StreamKind},
    pipeline::{ActivePipeline, FrameWaitError, InactivePipeline},
    processing_blocks::colorizer::Colorizer,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    convert::TryFrom,
    env,
    net::SocketAddr,
    str::FromStr,
    sync::{Arc, Condvar, Mutex},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tower_http::services::{ServeDir, ServeFile};
use tracing::{error, info, warn};

const BOUNDARY: &str = "frame";
const JPEG_QUALITY: u8 = 80;
const STREAM_IDS: [StreamId; 4] = [
    StreamId::Color,
    StreamId::IrLeft,
    StreamId::IrRight,
    StreamId::Depth,
];

#[derive(Clone)]
struct AppState {
    pump: Arc<RealSensePump>,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum StreamId {
    Color,
    IrLeft,
    IrRight,
    Depth,
}

impl StreamId {
    fn as_str(self) -> &'static str {
        match self {
            Self::Color => "color",
            Self::IrLeft => "ir-left",
            Self::IrRight => "ir-right",
            Self::Depth => "depth",
        }
    }
}

impl FromStr for StreamId {
    type Err = ();

    fn from_str(value: &str) -> std::result::Result<Self, Self::Err> {
        match value {
            "color" => Ok(Self::Color),
            "ir-left" => Ok(Self::IrLeft),
            "ir-right" => Ok(Self::IrRight),
            "depth" => Ok(Self::Depth),
            _ => Err(()),
        }
    }
}

#[derive(Clone)]
struct EncodedFrame {
    jpeg: Vec<u8>,
    sequence: u64,
}

struct FrameSnapshot {
    jpeg: Vec<u8>,
    sequence: u64,
}

#[derive(Serialize)]
struct ErrorResponse {
    error: String,
}

#[derive(Serialize)]
struct RunningResponse {
    running: bool,
}

#[derive(Deserialize)]
struct ConfigQuery {
    width: Option<usize>,
    height: Option<usize>,
    fps: Option<usize>,
    preset: Option<String>,
}

#[derive(Serialize)]
struct ConfigResponse {
    width: usize,
    height: usize,
    fps: usize,
    preset: String,
}

#[derive(Serialize)]
struct HealthResponse {
    streams: Vec<&'static str>,
    running: bool,
    fps: HashMap<StreamId, f64>,
    error: Option<String>,
}

struct RealSensePump {
    inner: Mutex<PumpInner>,
    cond: Condvar,
}

struct PumpInner {
    worker: Option<JoinHandle<()>>,
    stop_requested: bool,
    running: bool,
    width: usize,
    height: usize,
    fps: usize,
    preset: String,
    pending_preset: Option<String>,
    latest: HashMap<StreamId, EncodedFrame>,
    fps_counts: HashMap<StreamId, u64>,
    fps_latest: HashMap<StreamId, f64>,
    fps_window_start: Instant,
    last_error: Option<String>,
}

impl RealSensePump {
    fn new() -> Self {
        Self {
            inner: Mutex::new(PumpInner {
                worker: None,
                stop_requested: false,
                running: false,
                width: 640,
                height: 480,
                fps: 30,
                preset: "default".to_string(),
                pending_preset: None,
                latest: HashMap::new(),
                fps_counts: zero_counts(),
                fps_latest: zero_fps(),
                fps_window_start: Instant::now(),
                last_error: None,
            }),
            cond: Condvar::new(),
        }
    }

    fn start(self: &Arc<Self>) {
        self.reap_stopped_worker();

        let mut inner = self.inner.lock().unwrap();
        if inner.worker.is_some() {
            return;
        }

        inner.stop_requested = false;
        inner.running = true;
        inner.last_error = None;
        inner.pending_preset = Some(inner.preset.clone());
        inner.fps_counts = zero_counts();
        inner.fps_latest = zero_fps();
        inner.fps_window_start = Instant::now();

        let pump = Arc::clone(self);
        inner.worker = Some(thread::spawn(move || pump.run_worker()));
    }

    fn stop(&self) {
        let worker = {
            let mut inner = self.inner.lock().unwrap();
            inner.stop_requested = true;
            inner.running = false;
            inner.last_error = None;
            self.cond.notify_all();
            inner.worker.take()
        };

        if let Some(worker) = worker {
            let _ = worker.join();
        }

        let mut inner = self.inner.lock().unwrap();
        inner.latest.clear();
        inner.fps_counts = zero_counts();
        inner.fps_latest = zero_fps();
        self.cond.notify_all();
    }

    fn configure(
        self: &Arc<Self>,
        width: usize,
        height: usize,
        fps: usize,
        preset: String,
    ) -> Result<()> {
        validate_profile(width, height, fps)?;
        validate_preset(&preset)?;

        let worker = {
            let mut inner = self.inner.lock().unwrap();
            let restart = inner.running
                && inner.worker.is_some()
                && (inner.width != width || inner.height != height || inner.fps != fps);

            inner.width = width;
            inner.height = height;
            inner.fps = fps;
            inner.preset = preset.clone();
            inner.pending_preset = Some(preset);

            if restart {
                inner.stop_requested = true;
                inner.worker.take()
            } else {
                None
            }
        };

        if let Some(worker) = worker {
            let _ = worker.join();

            let mut inner = self.inner.lock().unwrap();
            if inner.running {
                inner.stop_requested = false;
                inner.pending_preset = Some(inner.preset.clone());
                inner.fps_counts = zero_counts();
                inner.fps_latest = zero_fps();
                inner.fps_window_start = Instant::now();

                let pump = Arc::clone(self);
                inner.worker = Some(thread::spawn(move || pump.run_worker()));
            }
        }

        Ok(())
    }

    fn is_running(&self) -> bool {
        self.inner.lock().unwrap().running
    }

    fn health(&self) -> HealthResponse {
        let inner = self.inner.lock().unwrap();
        HealthResponse {
            streams: STREAM_IDS.iter().map(|id| id.as_str()).collect(),
            running: inner.running,
            fps: inner.fps_latest.clone(),
            error: inner.last_error.clone(),
        }
    }

    fn wait_frame(
        &self,
        stream_id: StreamId,
        last_sequence: u64,
        timeout: Duration,
    ) -> Option<FrameSnapshot> {
        let deadline = Instant::now() + timeout;
        let mut inner = self.inner.lock().unwrap();

        loop {
            if let Some(frame) = inner.latest.get(&stream_id) {
                if frame.sequence != last_sequence {
                    return Some(FrameSnapshot {
                        jpeg: frame.jpeg.clone(),
                        sequence: frame.sequence,
                    });
                }
            }

            if !inner.running {
                return None;
            }

            let now = Instant::now();
            if now >= deadline {
                return None;
            }

            let remaining = deadline.saturating_duration_since(now);
            let (next_inner, wait_result) = self.cond.wait_timeout(inner, remaining).unwrap();
            inner = next_inner;
            if wait_result.timed_out() {
                return None;
            }
        }
    }

    fn reap_stopped_worker(&self) {
        let worker = {
            let mut inner = self.inner.lock().unwrap();
            if inner.worker.is_some() && !inner.running {
                inner.worker.take()
            } else {
                None
            }
        };

        if let Some(worker) = worker {
            let _ = worker.join();
        }
    }

    fn should_stop(&self) -> bool {
        self.inner.lock().unwrap().stop_requested
    }

    fn take_pending_preset(&self) -> Option<String> {
        let mut inner = self.inner.lock().unwrap();
        inner.pending_preset.take()
    }

    fn publish(&self, updates: HashMap<StreamId, Vec<u8>>) {
        if updates.is_empty() {
            return;
        }

        let mut inner = self.inner.lock().unwrap();
        for (stream_id, jpeg) in updates {
            let next_sequence = inner
                .latest
                .get(&stream_id)
                .map_or(1, |frame| frame.sequence + 1);
            inner.latest.insert(
                stream_id,
                EncodedFrame {
                    jpeg,
                    sequence: next_sequence,
                },
            );
            *inner.fps_counts.entry(stream_id).or_insert(0) += 1;
        }

        let elapsed = inner.fps_window_start.elapsed().as_secs_f64();
        if elapsed >= 1.0 {
            let counts = inner.fps_counts.clone();
            for stream_id in STREAM_IDS {
                let count = counts.get(&stream_id).copied().unwrap_or(0);
                inner
                    .fps_latest
                    .insert(stream_id, ((count as f64 / elapsed) * 10.0).round() / 10.0);
            }
            inner.fps_counts = zero_counts();
            inner.fps_window_start = Instant::now();
        }

        self.cond.notify_all();
    }

    fn mark_stopped(&self, error: Option<String>) {
        let mut inner = self.inner.lock().unwrap();
        inner.running = false;
        inner.last_error = error;
        inner.fps_counts = zero_counts();
        inner.fps_latest = zero_fps();
        self.cond.notify_all();
    }

    fn run_worker(self: Arc<Self>) {
        let (width, height, fps) = {
            let inner = self.inner.lock().unwrap();
            (inner.width, inner.height, inner.fps)
        };

        let context = match RealSenseContext::new() {
            Ok(context) => context,
            Err(error) => {
                self.mark_stopped(Some(format!(
                    "Failed to initialize RealSense context: {error}"
                )));
                return;
            }
        };

        let devices = context.query_devices(HashSet::new());
        if devices.is_empty() {
            self.mark_stopped(Some(
                "No RealSense device connected or available".to_string(),
            ));
            return;
        }
        drop(devices);

        let mut active_pipeline = None;
        let mut start_error = None;
        for attempt in 1..=3 {
            if self.should_stop() {
                return;
            }

            match start_pipeline(&context, width, height, fps) {
                Ok(pipeline) => {
                    active_pipeline = Some(pipeline);
                    break;
                }
                Err(error) => {
                    let message = error.to_string();
                    warn!(
                        "pipeline.start attempt {attempt}/3 failed at {width}x{height} @{fps}fps: {message}"
                    );
                    start_error = Some(message);
                    thread::sleep(Duration::from_millis(500));
                }
            }
        }

        let Some(mut pipeline) = active_pipeline else {
            if !self.should_stop() {
                let message = start_error
                    .map(|error| format!("Failed to start RealSense pipeline: {error}"))
                    .unwrap_or_else(|| "Failed to start RealSense pipeline".to_string());
                error!("{message}");
                self.mark_stopped(Some(message));
            }
            return;
        };

        let mut colorizer = match Colorizer::new(5) {
            Ok(colorizer) => colorizer,
            Err(error) => {
                let message = format!("Failed to initialize RealSense colorizer: {error}");
                error!("{message}");
                self.mark_stopped(Some(message));
                let _ = pipeline.stop();
                return;
            }
        };

        if let Some(preset) = self.take_pending_preset() {
            apply_depth_preset(&pipeline, &preset);
        }

        info!("RealSense pipeline started at {width}x{height} @{fps}fps");

        while !self.should_stop() {
            if let Some(preset) = self.take_pending_preset() {
                apply_depth_preset(&pipeline, &preset);
            }

            let frames = match pipeline.wait(Some(Duration::from_millis(1000))) {
                Ok(frames) => frames,
                Err(FrameWaitError::DidTimeoutBeforeFrameArrival) => continue,
                Err(error) => {
                    if !self.should_stop() {
                        let message = format!("RealSense frame wait failed: {error}");
                        error!("{message}");
                        self.mark_stopped(Some(message));
                    }
                    break;
                }
            };

            let mut updates = HashMap::new();

            let color_frames: Vec<ColorFrame> = frames.frames_of_type();
            if let Some(frame) = color_frames.first() {
                if let Some(jpeg) = encode_color_frame(frame) {
                    updates.insert(StreamId::Color, jpeg);
                }
            }

            let infrared_frames: Vec<InfraredFrame> = frames.frames_of_type();
            if let Some(frame) = infrared_frames.first() {
                if let Some(jpeg) = encode_luma_frame(frame) {
                    updates.insert(StreamId::IrLeft, jpeg);
                }
            }
            if let Some(frame) = infrared_frames.get(1) {
                if let Some(jpeg) = encode_luma_frame(frame) {
                    updates.insert(StreamId::IrRight, jpeg);
                }
            }

            let depth_frames: Vec<DepthFrame> = frames.frames_of_type();
            if let Some(depth_frame) = depth_frames.into_iter().next() {
                if colorizer.queue(depth_frame).is_ok() {
                    if let Ok(colorized) = colorizer.wait(Duration::from_millis(100)) {
                        if let Some(jpeg) = encode_color_frame(&colorized) {
                            updates.insert(StreamId::Depth, jpeg);
                        }
                    }
                }
            }

            self.publish(updates);
        }

        let _ = pipeline.stop();
        info!("RealSense pipeline stopped");
    }
}

fn zero_counts() -> HashMap<StreamId, u64> {
    STREAM_IDS.into_iter().map(|id| (id, 0)).collect()
}

fn zero_fps() -> HashMap<StreamId, f64> {
    STREAM_IDS.into_iter().map(|id| (id, 0.0)).collect()
}

fn preset_value(preset: &str) -> Option<f32> {
    match preset {
        "default" => Some(1.0),
        "hand" => Some(2.0),
        "high-accuracy" => Some(3.0),
        "high-density" => Some(4.0),
        "medium-density" => Some(5.0),
        _ => None,
    }
}

fn validate_preset(preset: &str) -> Result<()> {
    preset_value(preset)
        .map(|_| ())
        .ok_or_else(|| anyhow!("Unknown preset: {preset}"))
}

fn validate_profile(width: usize, height: usize, fps: usize) -> Result<()> {
    if !(1..=8192).contains(&width) {
        return Err(anyhow!("width must be between 1 and 8192"));
    }
    if !(1..=8192).contains(&height) {
        return Err(anyhow!("height must be between 1 and 8192"));
    }
    if !(1..=300).contains(&fps) {
        return Err(anyhow!("fps must be between 1 and 300"));
    }
    Ok(())
}

fn start_pipeline(
    context: &RealSenseContext,
    width: usize,
    height: usize,
    fps: usize,
) -> Result<ActivePipeline> {
    let pipeline = InactivePipeline::try_from(context)?;
    let mut config = Config::new();
    config
        .enable_stream(
            Rs2StreamKind::Color,
            None,
            width,
            height,
            Rs2Format::Bgr8,
            fps,
        )?
        .enable_stream(
            Rs2StreamKind::Depth,
            None,
            width,
            height,
            Rs2Format::Z16,
            fps,
        )?
        .enable_stream(
            Rs2StreamKind::Infrared,
            Some(1),
            width,
            height,
            Rs2Format::Y8,
            fps,
        )?
        .enable_stream(
            Rs2StreamKind::Infrared,
            Some(2),
            width,
            height,
            Rs2Format::Y8,
            fps,
        )?;

    pipeline.start(Some(config))
}

fn apply_depth_preset(pipeline: &ActivePipeline, preset: &str) {
    let Some(value) = preset_value(preset) else {
        warn!("Unknown RealSense visual preset: {preset}");
        return;
    };

    for mut sensor in pipeline.profile().device().sensors() {
        if sensor.supports_option(Rs2Option::VisualPreset) {
            match sensor.set_option(Rs2Option::VisualPreset, value) {
                Ok(()) => info!("Applied RealSense visual preset: {preset}"),
                Err(error) => warn!("Failed to apply RealSense visual preset {preset}: {error}"),
            }
            return;
        }
    }
}

fn encode_color_frame(frame: &ColorFrame) -> Option<Vec<u8>> {
    let mut rgb = Vec::with_capacity(frame.width() * frame.height() * 3);
    for pixel in frame.iter() {
        match pixel {
            PixelKind::Bgr8 { b, g, r } => rgb.extend_from_slice(&[*r, *g, *b]),
            PixelKind::Bgra8 { b, g, r, .. } => rgb.extend_from_slice(&[*r, *g, *b]),
            PixelKind::Rgb8 { r, g, b } => rgb.extend_from_slice(&[*r, *g, *b]),
            PixelKind::Rgba8 { r, g, b, .. } => rgb.extend_from_slice(&[*r, *g, *b]),
            PixelKind::Y8 { y } | PixelKind::Raw8 { val: y } => {
                rgb.extend_from_slice(&[*y, *y, *y])
            }
            _ => rgb.extend_from_slice(&[0, 0, 0]),
        }
    }
    encode_jpeg(&rgb, frame.width(), frame.height(), ColorType::Rgb)
}

fn encode_luma_frame(frame: &InfraredFrame) -> Option<Vec<u8>> {
    let mut luma = Vec::with_capacity(frame.width() * frame.height());
    for pixel in frame.iter() {
        match pixel {
            PixelKind::Y8 { y } | PixelKind::Raw8 { val: y } => luma.push(*y),
            PixelKind::Y16 { y } => luma.push((*y >> 8) as u8),
            PixelKind::Bgr8 { b, g, r } | PixelKind::Rgb8 { r, g, b } => {
                luma.push((0.299 * *r as f32 + 0.587 * *g as f32 + 0.114 * *b as f32) as u8);
            }
            _ => luma.push(0),
        }
    }
    encode_jpeg(&luma, frame.width(), frame.height(), ColorType::Luma)
}

fn encode_jpeg(
    pixels: &[u8],
    width: usize,
    height: usize,
    color_type: ColorType,
) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let encoder = Encoder::new(&mut out, JPEG_QUALITY);
    encoder
        .encode(
            pixels,
            u16::try_from(width).ok()?,
            u16::try_from(height).ok()?,
            color_type,
        )
        .ok()?;
    Some(out)
}

fn make_mjpeg_part(frame: FrameSnapshot) -> Bytes {
    let mut part = Vec::with_capacity(frame.jpeg.len() + 128);
    part.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    part.extend_from_slice(b"Content-Type: image/jpeg\r\n");
    part.extend_from_slice(format!("Content-Length: {}\r\n\r\n", frame.jpeg.len()).as_bytes());
    part.extend_from_slice(&frame.jpeg);
    part.extend_from_slice(b"\r\n");
    Bytes::from(part)
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            env::var("RUST_LOG").unwrap_or_else(|_| "info,tower_http=warn,axum=warn".to_string()),
        )
        .init();

    let hostname = env::var("WENDY_HOSTNAME").unwrap_or_else(|_| "0.0.0.0".to_string());
    let state = AppState {
        pump: Arc::new(RealSensePump::new()),
    };

    let app = Router::new()
        .route("/start", post(start))
        .route("/stop", post(stop))
        .route("/config", post(configure))
        .route("/health", get(health))
        .route("/stream/{stream_id}", get(stream_endpoint))
        .fallback_service(ServeDir::new("static").fallback(ServeFile::new("static/index.html")))
        .with_state(state);

    let addr: SocketAddr = "0.0.0.0:{{.PORT}}".parse().unwrap();
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("RealSense Rust server running on http://{hostname}:{{.PORT}}");
    axum::serve(listener, app)
        .await
        .context("Axum server failed")
}

async fn start(State(state): State<AppState>) -> Json<RunningResponse> {
    state.pump.start();
    Json(RunningResponse {
        running: state.pump.is_running(),
    })
}

async fn stop(State(state): State<AppState>) -> Json<RunningResponse> {
    state.pump.stop();
    Json(RunningResponse {
        running: state.pump.is_running(),
    })
}

async fn configure(State(state): State<AppState>, Query(query): Query<ConfigQuery>) -> Response {
    let width = query.width.unwrap_or(640);
    let height = query.height.unwrap_or(480);
    let fps = query.fps.unwrap_or(30);
    let preset = query.preset.unwrap_or_else(|| "default".to_string());

    match state.pump.configure(width, height, fps, preset.clone()) {
        Ok(()) => Json(ConfigResponse {
            width,
            height,
            fps,
            preset,
        })
        .into_response(),
        Err(error) => (
            StatusCode::BAD_REQUEST,
            Json(ErrorResponse {
                error: error.to_string(),
            }),
        )
            .into_response(),
    }
}

async fn health(State(state): State<AppState>) -> Json<HealthResponse> {
    Json(state.pump.health())
}

async fn stream_endpoint(Path(stream_id): Path<String>, State(state): State<AppState>) -> Response {
    let Ok(stream_id) = StreamId::from_str(&stream_id) else {
        return (
            StatusCode::NOT_FOUND,
            Json(ErrorResponse {
                error: "Unknown stream".to_string(),
            }),
        )
            .into_response();
    };

    let pump = state.pump.clone();
    let body_stream = stream! {
        let mut last_sequence = 0;
        loop {
            let pump = pump.clone();
            let frame = tokio::task::spawn_blocking(move || {
                pump.wait_frame(stream_id, last_sequence, Duration::from_secs(5))
            })
            .await;

            let Ok(Some(frame)) = frame else {
                break;
            };

            last_sequence = frame.sequence;
            yield Ok::<Bytes, std::io::Error>(make_mjpeg_part(frame));
        }
    };

    Response::builder()
        .header(
            header::CONTENT_TYPE,
            HeaderValue::from_static("multipart/x-mixed-replace; boundary=frame"),
        )
        .header(header::CACHE_CONTROL, HeaderValue::from_static("no-store"))
        .body(Body::from_stream(body_stream))
        .unwrap()
}
