import Foundation
import Hummingbird
@preconcurrency import MLXLLM
@preconcurrency import MLXLMCommon
import MLXHuggingFace
import HuggingFace
import Tokenizers

struct ChatMessage: Decodable, Sendable {
    let role: String
    let content: String
}

struct ChatRequest: Decodable, Sendable {
    let messages: [ChatMessage]
    let system: String?
    let maxTokens: Int?
    let temperature: Double?
    let topP: Double?
    let topK: Int?
}

struct ChatResponse: ResponseEncodable {
    let reply: String
    let model: String
    let durationMs: Int64
}

struct HealthResponse: ResponseEncodable {
    let status: String
    let model: String
}

actor ChatManager {
    private nonisolated(unsafe) var session: ChatSession?
    private let modelId: String

    init(modelId: String) {
        self.modelId = modelId
    }

    func load() async throws {
        print("Loading model: \(modelId)...")
        // Self-contained model loading: MLXHuggingFace's #huggingFaceLoadModel
        // macro wires up a default HuggingFace.HubClient() downloader and a
        // Tokenizers.AutoTokenizer-backed TokenizerLoader, so this downloads
        // (and caches) the model + tokenizer from the Hub by id.
        let model = try await #huggingFaceLoadModel(configuration: ModelConfiguration(id: modelId))
        session = ChatSession(model)
        print("Model loaded successfully!")
    }

    func respond(
        to prompt: String,
        maxTokens: Int?,
        temperature: Double?,
        topP: Double?,
        topK: Int?
    ) async throws -> (String, Int64) {
        guard let session = session else {
            throw ChatError.notLoaded
        }

        // Mirror the gguf server's request-field -> sampling-param mapping
        // (same defaults/clamps as LlamaConfig + LlamaRunner.generate) so the
        // frontend's sampling sliders have the same effect on both backends.
        let tokens = clamp(maxTokens ?? 256, min: 1, max: 1024)
        let requestTemperature = Float(clamp(temperature ?? 0.6, min: 0.0, max: 2.0))
        let requestTopP = Float(clamp(topP ?? 0.95, min: 0.0, max: 1.0))
        let requestTopK = clamp(topK ?? 20, min: 0, max: 200)

        session.generateParameters = GenerateParameters(
            maxTokens: tokens,
            temperature: requestTemperature,
            topP: requestTopP,
            topK: requestTopK
        )

        let startTime = DispatchTime.now()
        let response = try await session.respond(to: prompt)
        let endTime = DispatchTime.now()
        let elapsedMs = Int64((endTime.uptimeNanoseconds - startTime.uptimeNanoseconds) / 1_000_000)

        return (response, elapsedMs)
    }

    enum ChatError: Error {
        case notLoaded
    }
}

func clamp<T: Comparable>(_ value: T, min lower: T, max upper: T) -> T {
    return min(max(value, lower), upper)
}

func readEnvInt(_ key: String, default value: Int, env: [String: String]) -> Int {
    if let raw = env[key], let parsed = Int(raw) {
        return parsed
    }
    return value
}

/// Builds a single prompt string from the request's system prompt + message
/// history, mirroring the gguf server's prompt construction so both backends
/// behave identically for the shared frontend.
func buildPrompt(system: String?, messages: [ChatMessage]) -> String {
    var sections: [String] = []
    let systemPrompt = (system ?? "You are a helpful assistant.").trimmingCharacters(in: .whitespacesAndNewlines)
    if !systemPrompt.isEmpty {
        sections.append("System: \(systemPrompt)")
    }

    for message in messages {
        let role = message.role.lowercased()
        let label: String
        switch role {
        case "assistant":
            label = "Assistant"
        case "system":
            label = "System"
        default:
            label = "User"
        }
        sections.append("\(label): \(message.content)")
    }

    sections.append("Assistant:")
    return sections.joined(separator: "\n\n")
}

@main
struct MLXLLMServer {
    static func main() async throws {
        let env = ProcessInfo.processInfo.environment
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let modelId = ProcessInfo.processInfo.environment["MODEL_ID"] ?? "mlx-community/TinyLlama-1.1B-Chat-v1.0-4bit"
        let port = readEnvInt("PORT", default: {{.PORT}}, env: env)

        // Determine frontend dist path
        let envPath = ProcessInfo.processInfo.environment["FRONTEND_DIST"]
        let containerPath = "/app/frontend/dist"
        let cwdPath = FileManager.default.currentDirectoryPath + "/../frontend/dist"

        let frontendDist: String
        if let env = envPath {
            frontendDist = env
        } else if FileManager.default.fileExists(atPath: containerPath + "/index.html") {
            frontendDist = containerPath
        } else {
            frontendDist = cwdPath
        }

        print("Serving frontend from: \(frontendDist)")
        print("Using model: \(modelId)")

        // Load the model
        let chatManager = ChatManager(modelId: modelId)
        try await chatManager.load()

        let router = Router()

        // Health check endpoint
        router.get("/health") { _, _ -> HealthResponse in
            HealthResponse(status: "ok", model: modelId)
        }

        // Chat endpoint
        router.post("/api/chat") { request, context async throws -> ChatResponse in
            let body = try await request.decode(as: ChatRequest.self, context: context)
            let prompt = buildPrompt(system: body.system, messages: body.messages)

            let (reply, durationMs) = try await chatManager.respond(
                to: prompt,
                maxTokens: body.maxTokens,
                temperature: body.temperature,
                topP: body.topP,
                topK: body.topK
            )

            return ChatResponse(
                reply: reply,
                model: modelId,
                durationMs: durationMs
            )
        }

        // Serve static files from frontend dist
        router.add(middleware: FileMiddleware(frontendDist, searchForIndexHtml: true))

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: port))
        )

        print("Server running on http://\(hostname):\(port)")
        try await app.runService()
    }
}
