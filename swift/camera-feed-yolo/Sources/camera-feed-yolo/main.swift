import Foundation
import Hummingbird
import HummingbirdWebSocket
import COnnxRuntime
import CTurboJPEG

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

private let kInputSize: Int32 = 640

private let kCocoNames: [String] = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog",
    "horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella",
    "handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite",
    "baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle",
    "wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich",
    "orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch",
    "potted plant","bed","dining table","toilet","tv","laptop","mouse","remote",
    "keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book",
    "clock","vase","scissors","teddy bear","hair drier","toothbrush",
]

private func envTruthy(_ name: String) -> Bool {
    let v = (ProcessInfo.processInfo.environment[name] ?? "").lowercased()
    return v == "true" || v == "1" || v == "yes"
}

private func isRpi() -> Bool {
    let dev = ProcessInfo.processInfo.environment["WENDY_DEVICE_TYPE"] ?? ""
    if dev.hasPrefix("raspberrypi") { return true }
    if !dev.isEmpty { return false }
    if let model = try? String(contentsOfFile: "/proc/device-tree/model", encoding: .utf8) {
        return model.contains("Raspberry Pi")
    }
    return false
}

// ───────────────────────────────────────────────────────────────────────────
// Box / Meta
// ───────────────────────────────────────────────────────────────────────────

struct Box: Codable, Sendable {
    var x1: Float
    var y1: Float
    var x2: Float
    var y2: Float
    var conf: Float
    var cls: Int
    var name: String
}

struct Meta: Codable, Sendable {
    var detections: Int
    var inference_ms: Double
    var classes: [String: Int]
    var boxes: [Box]
    var frame_w: Int
    var frame_h: Int

    static let empty = Meta(detections: 0, inference_ms: 0, classes: [:], boxes: [], frame_w: 0, frame_h: 0)
}

private func iou(_ a: Box, _ b: Box) -> Float {
    let ix1 = max(a.x1, b.x1), iy1 = max(a.y1, b.y1)
    let ix2 = min(a.x2, b.x2), iy2 = min(a.y2, b.y2)
    let iw = max(0, ix2 - ix1), ih = max(0, iy2 - iy1)
    let inter = iw * ih
    let aa = max(0, a.x2 - a.x1) * max(0, a.y2 - a.y1)
    let bb = max(0, b.x2 - b.x1) * max(0, b.y2 - b.y1)
    let u = aa + bb - inter
    return u > 0 ? inter / u : 0
}

// ───────────────────────────────────────────────────────────────────────────
// YoloEngine — wraps the ONNX Runtime C API.
// ───────────────────────────────────────────────────────────────────────────

private func ortApi() -> UnsafePointer<OrtApi> {
    let base = OrtGetApiBase()!
    let getApi = base.pointee.GetApi!
    return getApi(UInt32(ORT_API_VERSION))!
}

private func checkStatus(_ status: OpaquePointer?, _ api: UnsafePointer<OrtApi>) throws {
    guard let s = status else { return }
    defer { api.pointee.ReleaseStatus(s) }
    let cstr = api.pointee.GetErrorMessage(s)
    let msg = cstr.map { String(cString: $0) } ?? "unknown ONNX Runtime error"
    throw NSError(domain: "ort", code: 1, userInfo: [NSLocalizedDescriptionKey: msg])
}

actor YoloEngine {
    private let api: UnsafePointer<OrtApi>
    private var env: OpaquePointer?
    private var session: OpaquePointer?
    private var memInfo: OpaquePointer?
    private var allocator: OpaquePointer?
    private var inputName: UnsafeMutablePointer<CChar>?
    private var outputName: UnsafeMutablePointer<CChar>?
    private var inputNameStr: String = "images"
    private var outputNameStr: String = "output0"
    private let decoder: tjhandle?

    init(modelPath: String, useGpu: Bool) throws {
        self.api = ortApi()
        self.decoder = tjInitDecompress()

        var envPtr: OpaquePointer?
        try checkStatus(api.pointee.CreateEnv(ORT_LOGGING_LEVEL_WARNING, "yolo", &envPtr), api)
        self.env = envPtr

        var optsPtr: OpaquePointer?
        try checkStatus(api.pointee.CreateSessionOptions(&optsPtr), api)
        defer { if let o = optsPtr { api.pointee.ReleaseSessionOptions(o) } }

        try checkStatus(api.pointee.SetIntraOpNumThreads(optsPtr, 2), api)
        try checkStatus(api.pointee.SetSessionGraphOptimizationLevel(optsPtr, ORT_ENABLE_ALL), api)

        if useGpu {
            // CUDA EP via the V2 options struct (rolls back gracefully if the
            // runtime wasn't built with CUDA support).
            var cudaOpts: OpaquePointer?
            if api.pointee.CreateCUDAProviderOptions(&cudaOpts) == nil, let cuda = cudaOpts {
                _ = api.pointee.SessionOptionsAppendExecutionProvider_CUDA_V2(optsPtr, cuda)
                api.pointee.ReleaseCUDAProviderOptions(cuda)
                print("[yolo] CUDA execution provider requested")
            } else {
                print("[yolo] CUDA EP unavailable — using CPU")
            }
        } else {
            print("[yolo] using CPU execution provider")
        }

        var sessionPtr: OpaquePointer?
        try modelPath.withCString { cstr in
            try checkStatus(api.pointee.CreateSession(envPtr, cstr, optsPtr, &sessionPtr), api)
        }
        self.session = sessionPtr

        var allocPtr: OpaquePointer?
        try checkStatus(api.pointee.GetAllocatorWithDefaultOptions(&allocPtr), api)
        self.allocator = allocPtr

        var inName: UnsafeMutablePointer<CChar>?
        try checkStatus(api.pointee.SessionGetInputName(sessionPtr, 0, allocPtr, &inName), api)
        self.inputName = inName
        if let n = inName { self.inputNameStr = String(cString: n) }

        var outName: UnsafeMutablePointer<CChar>?
        try checkStatus(api.pointee.SessionGetOutputName(sessionPtr, 0, allocPtr, &outName), api)
        self.outputName = outName
        if let n = outName { self.outputNameStr = String(cString: n) }

        var memPtr: OpaquePointer?
        try checkStatus(api.pointee.CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &memPtr), api)
        self.memInfo = memPtr
    }

    deinit {
        if let d = decoder { tjDestroy(d) }
        if let n = inputName, let a = allocator { _ = api.pointee.AllocatorFree(a, n) }
        if let n = outputName, let a = allocator { _ = api.pointee.AllocatorFree(a, n) }
        if let m = memInfo { api.pointee.ReleaseMemoryInfo(m) }
        if let s = session { api.pointee.ReleaseSession(s) }
        if let e = env { api.pointee.ReleaseEnv(e) }
    }

    func infer(jpeg: Data, confThreshold: Float) -> (boxes: [Box], width: Int, height: Int)? {
        guard let session = session, let memInfo = memInfo, let decoder = decoder else { return nil }

        // Decode JPEG -> RGB.
        var w: Int32 = 0, h: Int32 = 0, subsamp: Int32 = 0, colorspace: Int32 = 0
        let decodeHeader = jpeg.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Int32 in
            let ptr = raw.bindMemory(to: UInt8.self).baseAddress
            return tjDecompressHeader3(decoder, ptr, UInt(jpeg.count), &w, &h, &subsamp, &colorspace)
        }
        guard decodeHeader == 0, w > 0, h > 0 else { return nil }

        var rgb = [UInt8](repeating: 0, count: Int(w) * Int(h) * 3)
        let decodeOk = jpeg.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Int32 in
            let ptr = raw.bindMemory(to: UInt8.self).baseAddress
            return rgb.withUnsafeMutableBufferPointer { dst in
                tjDecompress2(decoder, ptr, UInt(jpeg.count), dst.baseAddress, w, 0, h, Int32(TJPF_RGB.rawValue), 0)
            }
        }
        guard decodeOk == 0 else { return nil }

        // Letterbox to kInputSize x kInputSize.
        let scale = min(Float(kInputSize) / Float(w), Float(kInputSize) / Float(h))
        let newW = Int(round(Float(w) * scale))
        let newH = Int(round(Float(h) * scale))
        let padX = (Int(kInputSize) - newW) / 2
        let padY = (Int(kInputSize) - newH) / 2

        let plane = Int(kInputSize) * Int(kInputSize)
        var input = [Float](repeating: 114.0 / 255.0, count: 3 * plane)
        let stride = Int(w) * 3
        for y in 0..<newH {
            let srcY = min(Int((Float(y) + 0.5) / scale), Int(h) - 1)
            let rowOff = srcY * stride
            for x in 0..<newW {
                let srcX = min(Int((Float(x) + 0.5) / scale), Int(w) - 1)
                let idx = rowOff + srcX * 3
                let dstIdx = (padY + y) * Int(kInputSize) + (padX + x)
                input[0 * plane + dstIdx] = Float(rgb[idx]) / 255.0
                input[1 * plane + dstIdx] = Float(rgb[idx + 1]) / 255.0
                input[2 * plane + dstIdx] = Float(rgb[idx + 2]) / 255.0
            }
        }

        // Build the input tensor.
        var shape: [Int64] = [1, 3, Int64(kInputSize), Int64(kInputSize)]
        var inputTensor: OpaquePointer?
        let tensorOk = input.withUnsafeMutableBufferPointer { ibuf -> Bool in
            shape.withUnsafeMutableBufferPointer { sbuf in
                let st = api.pointee.CreateTensorWithDataAsOrtValue(
                    memInfo,
                    ibuf.baseAddress,
                    UInt(ibuf.count) * UInt(MemoryLayout<Float>.size),
                    sbuf.baseAddress,
                    UInt(sbuf.count),
                    ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT,
                    &inputTensor)
                return st == nil
            }
        }
        guard tensorOk, let inTensor = inputTensor else { return nil }
        defer { api.pointee.ReleaseValue(inTensor) }

        // Build the const-char-pointer arrays Swift-side; ORT expects
        // (input_names, inputs, input_len, output_names, output_names_len, outputs).
        var inNames: [UnsafePointer<CChar>?] = [inputName.map { UnsafePointer($0) }]
        var outNames: [UnsafePointer<CChar>?] = [outputName.map { UnsafePointer($0) }]
        var inValues: [OpaquePointer?] = [inTensor]
        var outValues: [OpaquePointer?] = [nil]

        let runOk = inNames.withUnsafeBufferPointer { inNamesBuf -> Bool in
            outNames.withUnsafeBufferPointer { outNamesBuf -> Bool in
                inValues.withUnsafeBufferPointer { inValuesBuf -> Bool in
                    outValues.withUnsafeMutableBufferPointer { outValuesBuf -> Bool in
                        let st = api.pointee.Run(
                            session,
                            nil,
                            inNamesBuf.baseAddress,
                            inValuesBuf.baseAddress,
                            1,
                            outNamesBuf.baseAddress,
                            1,
                            outValuesBuf.baseAddress)
                        return st == nil
                    }
                }
            }
        }
        guard runOk, let outVal = outValues[0] else { return nil }
        defer { api.pointee.ReleaseValue(outVal) }

        // Read the output tensor — expected shape (1, 84, N).
        var info: OpaquePointer?
        guard api.pointee.GetTensorTypeAndShape(outVal, &info) == nil, let infoPtr = info else { return nil }
        defer { api.pointee.ReleaseTensorTypeAndShapeInfo(infoPtr) }

        var dimCount: UInt = 0
        _ = api.pointee.GetDimensionsCount(infoPtr, &dimCount)
        guard dimCount == 3 else { return nil }
        var dims = [Int64](repeating: 0, count: Int(dimCount))
        _ = dims.withUnsafeMutableBufferPointer { api.pointee.GetDimensions(infoPtr, $0.baseAddress, dimCount) }
        guard dims[1] >= 84 else { return nil }
        let numAnchors = Int(dims[2])

        var rawData: UnsafeMutableRawPointer?
        guard api.pointee.GetTensorMutableData(outVal, &rawData) == nil, let basePtr = rawData else { return nil }
        let preds = basePtr.bindMemory(to: Float.self, capacity: 84 * numAnchors)

        var candidates: [Box] = []
        candidates.reserveCapacity(256)
        for i in 0..<numAnchors {
            var bestCls = 0
            var bestScore: Float = 0
            for c in 0..<80 {
                let s = preds[(4 + c) * numAnchors + i]
                if s > bestScore { bestScore = s; bestCls = c }
            }
            if bestScore < confThreshold { continue }
            let cx = preds[0 * numAnchors + i]
            let cy = preds[1 * numAnchors + i]
            let bw = preds[2 * numAnchors + i]
            let bh = preds[3 * numAnchors + i]
            let x1 = max(0, min(Float(w - 1), (cx - bw / 2 - Float(padX)) / scale))
            let y1 = max(0, min(Float(h - 1), (cy - bh / 2 - Float(padY)) / scale))
            let x2 = max(0, min(Float(w - 1), (cx + bw / 2 - Float(padX)) / scale))
            let y2 = max(0, min(Float(h - 1), (cy + bh / 2 - Float(padY)) / scale))
            candidates.append(Box(x1: x1, y1: y1, x2: x2, y2: y2, conf: bestScore, cls: bestCls, name: kCocoNames[bestCls]))
        }
        candidates.sort { $0.conf > $1.conf }
        var kept: [Box] = []
        for c in candidates {
            if kept.contains(where: { $0.cls == c.cls && iou($0, c) > 0.45 }) { continue }
            kept.append(c)
            if kept.count >= 100 { break }
        }
        return (kept, Int(w), Int(h))
    }
}

struct JPEGFrameParser: Sendable {
    private var buffer = Data()
    mutating func append(_ data: Data) -> [Data] {
        // Cap before append so a malformed source can't grow the buffer past
        // the limit before the next reset.
        if buffer.count + data.count > 10_000_000 { buffer.removeAll() }
        buffer.append(data)
        var frames: [Data] = []
        while let range = findFrame() {
            frames.append(Data(buffer[range]))
            buffer.removeSubrange(buffer.startIndex...range.upperBound)
        }
        return frames
    }
    private func findFrame() -> ClosedRange<Int>? {
        guard buffer.count >= 4 else { return nil }
        var soi: Int?
        for i in buffer.startIndex..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD8 { soi = i; break }
        }
        guard let start = soi else { return nil }
        for i in (start + 2)..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD9 { return start...(i + 1) }
        }
        return nil
    }
}

// ───────────────────────────────────────────────────────────────────────────
// Camera info & enumeration
// ───────────────────────────────────────────────────────────────────────────

struct CameraInfo: Codable, Sendable {
    let id: String
    let name: String
}

func listCameras() -> [CameraInfo] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/v4l2-ctl")
    process.arguments = ["--list-devices"]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        return []
    }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    guard let output = String(data: data, encoding: .utf8) else { return [] }
    var cameras: [CameraInfo] = []
    var currentName: String?
    for line in output.components(separatedBy: "\n") {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if !line.hasPrefix("\t") && !line.hasPrefix(" ") && trimmed.hasSuffix(":") {
            currentName = String(trimmed.dropLast())
        } else if trimmed.hasPrefix("/dev/video") {
            cameras.append(CameraInfo(id: trimmed, name: currentName ?? trimmed))
        }
    }
    return cameras
}

// ───────────────────────────────────────────────────────────────────────────
// MJPEGCamera actor — owns the gst-launch-1.0 process + tracks subscribers.
// ───────────────────────────────────────────────────────────────────────────

actor MJPEGCamera {
    private var subscribers: [ObjectIdentifier: @Sendable (Data, String) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String
    private let usePassthrough: Bool
    private var latestJpeg: Data?
    private var latestMetaJson: String = "{\"detections\":0,\"inference_ms\":0,\"classes\":{},\"boxes\":[],\"frame_w\":0,\"frame_h\":0}"
    private var inferenceCallback: (@Sendable (Data) async -> Void)?

    init(device: String = "/dev/video0", usePassthrough: Bool) {
        self.currentDevice = device
        self.usePassthrough = usePassthrough
    }

    func setInferenceCallback(_ cb: @escaping @Sendable (Data) async -> Void) {
        self.inferenceCallback = cb
    }

    func updateMeta(_ json: String) {
        self.latestMetaJson = json
    }

    func currentMeta() -> String { latestMetaJson }

    func subscribe(id: ObjectIdentifier, handler: @escaping @Sendable (Data, String) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 { startPipeline() }
    }

    func unsubscribe(id: ObjectIdentifier) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty { stopPipeline() }
    }

    func switchCamera(to device: String) {
        guard device != currentDevice else { return }
        currentDevice = device
        if !subscribers.isEmpty { stopPipeline(); startPipeline() }
    }

    private func broadcast(_ frame: Data) async {
        latestJpeg = frame
        let meta = latestMetaJson
        let handlers = Array(subscribers.values)
        let cb = inferenceCallback

        await withTaskGroup(of: Void.self) { group in
            for handler in handlers {
                group.addTask {
                    await handler(frame, meta)
                }
            }
            if let cb {
                group.addTask {
                    await cb(frame)
                }
            }
        }
    }

    private func startPipeline() {
        let device = currentDevice
        let passthrough = usePassthrough
        pipelineTask = Task { [weak self] in
            guard let self else { return }
            var delayMs: UInt64 = 1000
            while !Task.isCancelled {
                let stillNeeded = await self.hasSubscribers()
                if !stillNeeded { return }
                do {
                    try await self.runGStreamerPipeline(device: device, passthrough: passthrough)
                } catch is CancellationError {
                    return
                } catch {
                    print("[gst] pipeline error: \(error)")
                }
                if Task.isCancelled { return }
                let nowNeeded = await self.hasSubscribers()
                if !nowNeeded { return }
                print("[gst] retrying pipeline in \(delayMs)ms")
                try? await Task.sleep(nanoseconds: delayMs * 1_000_000)
                delayMs = min(UInt64(Double(delayMs) * 1.5), 5000)
            }
        }
    }

    private func hasSubscribers() -> Bool { !subscribers.isEmpty }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    private func runGStreamerPipeline(device: String, passthrough: Bool) async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
        // Passthrough on RPi/CPU avoids a 30fps decode/re-encode brown-out under
        // GStreamer + inference load. Jetson keeps the decode/encode for quality
        // since it has hardware JPEG codecs.
        if passthrough {
            process.arguments = [
                "v4l2src", "device=\(device)", "!",
                "image/jpeg", "!",
                "fdsink", "fd=1",
            ]
        } else {
            process.arguments = [
                "v4l2src", "device=\(device)", "!",
                "image/jpeg", "!",
                "jpegdec", "!",
                "jpegenc", "quality=85", "!",
                "fdsink", "fd=1",
            ]
        }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()
        let handle = pipe.fileHandleForReading
        var parser = JPEGFrameParser()
        await withTaskCancellationHandler {
            while !Task.isCancelled {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                let frames = parser.append(chunk)
                for frame in frames {
                    await self.broadcast(frame)
                }
            }
            process.terminate()
        } onCancel: {
            process.terminate()
        }
    }
}

// ───────────────────────────────────────────────────────────────────────────
// Confidence holder (separate actor so it can be mutated from WS handler).
// ───────────────────────────────────────────────────────────────────────────

actor ConfidenceState {
    private var value: Float = 0.25
    func get() -> Float { value }
    func set(_ v: Float) { value = max(0.05, min(0.95, v)) }
}

// ───────────────────────────────────────────────────────────────────────────
// Application
// ───────────────────────────────────────────────────────────────────────────

private struct ClientCommand: Decodable {
    let switch_camera: String?
    let confidence: Float?
}

@main
struct CameraFeedYoloApp {
    static func main() async throws {
        let useGpu = envTruthy("WENDY_HAS_GPU")
        let rpi = isRpi()
        let usePassthrough = !useGpu || rpi
        let minIntervalMs: UInt64 = useGpu ? UInt64(1000 / 15) : UInt64(1000 / 3)

        print("[startup] platform=\(ProcessInfo.processInfo.environment["WENDY_PLATFORM"] ?? "unknown"), has_gpu=\(useGpu), is_rpi=\(rpi), capture=\(usePassthrough ? "passthrough" : "decode-encode")")

        let camera = MJPEGCamera(device: "/dev/video0", usePassthrough: usePassthrough)
        let confidence = ConfidenceState()

        let engine: YoloEngine
        do {
            engine = try YoloEngine(modelPath: "yolov8n.onnx", useGpu: useGpu)
            print("[yolo] model loaded")
        } catch {
            print("[yolo] failed to load model: \(error)")
            exit(1)
        }

        let pendingFrames = AsyncStream.makeStream(of: Data.self, bufferingPolicy: .bufferingNewest(1))
        let cont = pendingFrames.continuation
        await camera.setInferenceCallback { frame in
            cont.yield(frame)
        }
        Task.detached {
            var lastRun = Date.distantPast
            for await frame in pendingFrames.stream {
                let now = Date()
                let elapsedMs = UInt64(max(0, now.timeIntervalSince(lastRun) * 1000))
                if elapsedMs < minIntervalMs {
                    try? await Task.sleep(nanoseconds: (minIntervalMs - elapsedMs) * 1_000_000)
                }
                let conf = await confidence.get()
                let t0 = Date()
                let result = await engine.infer(jpeg: frame, confThreshold: conf)
                let inferenceMs = Date().timeIntervalSince(t0) * 1000
                lastRun = Date()
                guard let (boxes, w, h) = result else { continue }
                var classes: [String: Int] = [:]
                for b in boxes { classes[b.name, default: 0] += 1 }
                let meta = Meta(
                    detections: boxes.count,
                    inference_ms: (inferenceMs * 10).rounded() / 10,
                    classes: classes,
                    boxes: boxes,
                    frame_w: w,
                    frame_h: h)
                do {
                    let json = try JSONEncoder().encode(meta)
                    if let s = String(data: json, encoding: .utf8) {
                        await camera.updateMeta(s)
                    }
                } catch {
                    print("[yolo] meta encode failed: \(error)")
                }
            }
        }

        // ── HTTP routes ──
        let router = Router()
        router.get("/") { _, _ -> Response in
            let path = "index.html"
            guard FileManager.default.fileExists(atPath: path) else {
                return Response(status: .notFound)
            }
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(
                status: .ok,
                headers: [.contentType: "text/html; charset=utf-8"],
                body: .init(byteBuffer: buffer))
        }
        router.get("/assets/*") { request, _ -> Response in
            let requestPath = request.uri.path
            let prefix = "/assets/"
            guard requestPath.hasPrefix(prefix) else { return Response(status: .badRequest) }

            let relativePath = String(requestPath.dropFirst(prefix.count))
            guard !relativePath.isEmpty, !relativePath.hasPrefix("/") else {
                return Response(status: .badRequest)
            }

            let pathComponents = (relativePath as NSString).pathComponents
            guard !pathComponents.contains("..") else {
                return Response(status: .badRequest)
            }

            let assetsBaseURL = URL(fileURLWithPath: "./assets", isDirectory: true).standardizedFileURL
            let fileURL = assetsBaseURL.appendingPathComponent(relativePath).standardizedFileURL
            let assetsBasePath = assetsBaseURL.path.hasSuffix("/") ? assetsBaseURL.path : assetsBaseURL.path + "/"
            guard fileURL.path.hasPrefix(assetsBasePath) else {
                return Response(status: .badRequest)
            }

            let filePath = fileURL.path
            guard FileManager.default.fileExists(atPath: filePath) else { return Response(status: .notFound) }
            let data = try Data(contentsOf: fileURL)
            let ct: String = filePath.hasSuffix(".svg") ? "image/svg+xml"
                : filePath.hasSuffix(".png") ? "image/png"
                : filePath.hasSuffix(".jpg") || filePath.hasSuffix(".jpeg") ? "image/jpeg"
                : filePath.hasSuffix(".css") ? "text/css"
                : filePath.hasSuffix(".js") ? "application/javascript"
                : "application/octet-stream"
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(status: .ok, headers: [.contentType: ct], body: .init(byteBuffer: buffer))
        }
        router.get("/cameras") { _, _ -> Response in
            let cameras = listCameras()
            let data = try JSONEncoder().encode(cameras)
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(status: .ok, headers: [.contentType: "application/json"], body: .init(byteBuffer: buffer))
        }

        // ── WebSocket /stream ──
        let wsRouter = Router(context: BasicWebSocketRequestContext.self)
        wsRouter.ws("/stream") { inbound, outbound, _ in
            final class ConnectionID: Sendable {}
            let connID = ConnectionID()
            let id = ObjectIdentifier(connID)

            await camera.subscribe(id: id) { frame, metaJson in
                do {
                    try await outbound.write(.text(metaJson))
                    var buffer = ByteBufferAllocator().buffer(capacity: frame.count)
                    buffer.writeBytes(frame)
                    try await outbound.write(.binary(buffer))
                } catch {
                    print("[ws] write failed: \(error)")
                }
            }

            do {
                for try await message in inbound.messages(maxSize: 1_048_576) {
                    if case .text(let text) = message {
                        guard let data = text.data(using: .utf8) else { continue }
                        do {
                            let cmd = try JSONDecoder().decode(ClientCommand.self, from: data)
                            if let dev = cmd.switch_camera {
                                await camera.switchCamera(to: dev)
                            }
                            if let c = cmd.confidence {
                                await confidence.set(c)
                                print("[yolo] confidence -> \(c)")
                            }
                        } catch {
                            print("[ws] malformed client message: \(error)")
                        }
                    }
                }
            } catch {
                print("[ws] inbound loop error: \(error)")
            }

            await camera.unsubscribe(id: id)
        }

        let app = Application(
            router: router,
            server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        print("Camera feed (YOLO) running on http://0.0.0.0:{{.PORT}}")
        try await app.runService()
    }
}
