import Foundation
import Hummingbird
@preconcurrency import MLXLLM
@preconcurrency import MLXLMCommon

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

    func loadModel() async throws {
        print("Loading model: \(modelId)...")
        let model = try await MLXLMCommon.loadModel(id: modelId)
        session = ChatSession(model)
        print("Model loaded successfully!")
    }

    func respond(to prompt: String) async throws -> (String, Int64) {
        guard let session = session else {
            throw ChatError.notLoaded
        }

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
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let modelId = ProcessInfo.processInfo.environment["MODEL_ID"] ?? "mlx-community/TinyLlama-1.1B-Chat-v1.0-4bit"

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
        try await chatManager.loadModel()

        let router = Router()

        // Health check endpoint
        router.get("/health") { _, _ -> HealthResponse in
            HealthResponse(status: "ok", model: modelId)
        }

        // Chat endpoint
        router.post("/api/chat") { request, context async throws -> ChatResponse in
            let body = try await request.decode(as: ChatRequest.self, context: context)
            let prompt = buildPrompt(system: body.system, messages: body.messages)

            let (reply, durationMs) = try await chatManager.respond(to: prompt)

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
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        print("Server running on http://\(hostname):{{.PORT}}")
        try await app.runService()
    }
}
