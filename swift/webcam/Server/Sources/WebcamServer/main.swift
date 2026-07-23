import Foundation
import GStreamer
import Hummingbird
import HummingbirdWebSocket
import Logging
import NIOCore
import Synchronization

// MARK: - Video Settings

let frameWidth = 1280
let frameHeight = 720
let framerate = 30
let jpegQuality = 85

// MARK: - Response Types

struct StatusResponse: ResponseEncodable {
  let connectedClients: Int
  let cameraActive: Bool
  let settings: Settings

  struct Settings: Codable, Sendable {
    let width: Int
    let height: Int
    let framerate: Int
    let jpegQuality: Int
  }
}

// MARK: - Webcam Manager

actor WebcamManager {
  private var pipeline: Pipeline?
  private var frameTask: Task<Void, Never>?
  private var clients: [UUID: AsyncStream<[UInt8]>.Continuation] = [:]
  private let logger = Logger(label: "webcam")

  func addClient() -> (UUID, AsyncStream<[UInt8]>) {
    let id = UUID()
    let (stream, continuation) = AsyncStream<[UInt8]>.makeStream(
      bufferingPolicy: .bufferingNewest(2)
    )
    clients[id] = continuation

    if pipeline == nil {
      startPipeline()
    }

    return (id, stream)
  }

  func removeClient(_ id: UUID) {
    clients[id]?.finish()
    clients.removeValue(forKey: id)

    if clients.isEmpty {
      stopPipeline()
    }
  }

  private func startPipeline() {
    // Hardware pipeline for Jetson (NVJPEG encoder)
    let hwPipeline = """
      v4l2src device=/dev/video0 ! \
      video/x-raw,width=\(frameWidth),height=\(frameHeight),framerate=\(framerate)/1 ! \
      nvvidconv ! \
      video/x-raw(memory:NVMM) ! \
      nvjpegenc quality=\(jpegQuality) ! \
      appsink name=sink max-buffers=2 drop=true
      """

    // Software fallback for Linux (V4L2)
    let swPipeline = """
      v4l2src device=/dev/video0 ! \
      videoconvert ! videoscale add-borders=true ! videorate ! \
      video/x-raw,width=\(frameWidth),height=\(frameHeight),framerate=\(framerate)/1,format=I420 ! \
      jpegenc quality=\(jpegQuality) ! \
      appsink name=sink max-buffers=2 drop=true
      """

    // Cross-platform source selector (auto)
    let autoPipeline = """
      autovideosrc ! \
      videoconvert ! videoscale add-borders=true ! videorate ! \
      video/x-raw,width=\(frameWidth),height=\(frameHeight),framerate=\(framerate)/1,format=I420 ! \
      jpegenc quality=\(jpegQuality) ! \
      appsink name=sink max-buffers=2 drop=true
      """

    // macOS AVFoundation pipeline
    let macPipeline = """
      avfvideosrc device-index=0 ! \
      videoconvert ! videoscale method=lanczos ! aspectratiocrop aspect-ratio=16/9 ! videorate ! \
      video/x-raw,width=\(frameWidth),height=\(frameHeight),framerate=\(framerate)/1,format=I420 ! \
      jpegenc quality=\(jpegQuality) ! \
      appsink name=sink max-buffers=2 drop=true
      """

    func hasFactory(_ name: String) -> Bool {
      do {
        try GStreamer.initialize()
        _ = try Element.make(factory: name)
        return true
      } catch {
        return false
      }
    }

    // Prefer Linux pipelines first, then macOS, then auto.
    let candidates: [(name: String, desc: String, requires: [String])] = [
      ("hardware", hwPipeline, ["v4l2src", "nvvidconv", "nvjpegenc"]),
      ("software", swPipeline, ["v4l2src", "jpegenc"]),
      ("macOS", macPipeline, ["avfvideosrc", "jpegenc"]),
      ("auto", autoPipeline, ["autovideosrc", "jpegenc"])
    ]

    let available = candidates.filter { candidate in
      candidate.requires.allSatisfy(hasFactory)
    }
    let pipelines = (available.isEmpty ? candidates : available).map { ($0.name, $0.desc) }

    for (name, desc) in pipelines {
      do {
        let p = try Pipeline(desc)
        let sink = try p.appSink(named: "sink")
        try p.play()
        pipeline = p
        logger.info("Using \(name) JPEG encoder")

        frameTask = Task { [weak self] in
          do {
            for try await frame in sink.frames() {
              if Task.isCancelled { break }
              let jpegData = try frame.withUnsafeBytes { buffer in
                Array(UnsafeRawBufferPointer(start: buffer.baseAddress, count: buffer.count))
              }
              await self?.broadcastFrame(jpegData)
            }
          } catch {
            await self?.logError("Frame loop error: \(error)")
          }
        }

        return
      } catch {
        logger.debug("\(name) pipeline failed: \(error)")
      }
    }

    logger.error("No working GStreamer pipeline found")
  }

  private func broadcastFrame(_ data: [UInt8]) {
    for (_, continuation) in clients {
      continuation.yield(data)
    }
  }

  private func logError(_ message: String) {
    logger.error("\(message)")
  }

  private func stopPipeline() {
    frameTask?.cancel()
    frameTask = nil
    pipeline?.stop()
    pipeline = nil
    logger.info("Camera pipeline stopped")
  }

  var status: StatusResponse {
    StatusResponse(
      connectedClients: clients.count,
      cameraActive: pipeline != nil,
      settings: .init(
        width: frameWidth,
        height: frameHeight,
        framerate: framerate,
        jpegQuality: jpegQuality
      )
    )
  }
}

// MARK: - WebSocket Handler

func handleWebSocketStream(
  inbound: WebSocketInboundStream,
  outbound: WebSocketOutboundWriter,
  logger: Logger,
  webcam: WebcamManager
) async {
  logger.info("WebSocket client connected")
  let (clientId, frameStream) = await webcam.addClient()

  await withTaskGroup(of: Void.self) { group in
    // Send frames to client
    group.addTask {
      for await frameData in frameStream {
        do {
          var buffer = ByteBufferAllocator().buffer(capacity: frameData.count)
          buffer.writeBytes(frameData)
          try await outbound.write(.binary(buffer))
        } catch {
          break
        }
      }
    }

    // Detect client disconnect
    group.addTask {
      do {
        for try await _ in inbound {}
      } catch {
        // Client disconnected
      }
    }

    // When either completes, cancel the other
    _ = await group.next()
    group.cancelAll()
  }

  await webcam.removeClient(clientId)
  logger.info("WebSocket client disconnected")
}

// MARK: - Main

@main
struct WebcamServer {
  static let webcam = WebcamManager()

  static func main() async throws {
    let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "localhost"
    let port = {{.PORT}}
    let logger = Logger(label: "webcam-server")

    logger.info("GStreamer version: \(GStreamer.versionString)")

    // Determine static file paths
    let containerPath = "/app"
    let cwdPath = FileManager.default.currentDirectoryPath
    let staticRoot: String
    if FileManager.default.fileExists(atPath: containerPath + "/index.html") {
      staticRoot = containerPath
    } else {
      staticRoot = cwdPath
    }

    logger.info("Serving static files from: \(staticRoot)")

    let capturedWebcam = webcam

    let router = Router()

    // Serve static files with FileMiddleware
    router.middlewares.add(FileMiddleware(staticRoot, searchForIndexHtml: true))
    router.middlewares.add(LogRequestsMiddleware(.info))

    router.get("/status") { _, _ in
      await capturedWebcam.status
    }

    let app = Application(
      router: router,
      server: .http1WebSocketUpgrade { request, _, _ in
        guard request.path == "/stream" else {
          return .dontUpgrade
        }
        return .upgrade([:]) { inbound, outbound, _ in
          await handleWebSocketStream(
            inbound: inbound,
            outbound: outbound,
            logger: logger,
            webcam: capturedWebcam
          )
        }
      },
      configuration: .init(address: .hostname("0.0.0.0", port: port))
    )

    logger.info("Server running on http://\(hostname):\(port)")
    try await app.runService()
  }
}
