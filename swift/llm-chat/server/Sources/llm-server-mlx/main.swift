import Foundation
import Hummingbird
import MLXLLM
import MLXLMCommon

struct ChatRequest: Decodable, Sendable {
    let prompt: String
    let maxTokens: Int?
}

struct ChatResponse: ResponseEncodable {
    let text: String
    let tokensGenerated: Int
    let generationTimeMs: Int64
}

struct HealthResponse: ResponseEncodable {
    let status: String
    let model: String
}

actor ChatManager {
    private var session: ChatSession?
    private let modelId: String

    init(modelId: String) {
        self.modelId = modelId
    }

    func loadModel() async throws {
        print("Loading model: \(modelId)...")
        let model = try await loadModel(id: modelId)
        session = ChatSession(model)
        print("Model loaded successfully!")
    }

    func respond(to prompt: String) async throws -> (String, Int, Int64) {
        guard let session = session else {
            throw ChatError.notLoaded
        }

        let startTime = DispatchTime.now()
        var tokensGenerated = 0

        let response = try await session.respond(to: prompt) { tokens in
            tokensGenerated = tokens.count
            return .more
        }

        let endTime = DispatchTime.now()
        let elapsedMs = Int64((endTime.uptimeNanoseconds - startTime.uptimeNanoseconds) / 1_000_000)

        return (response, tokensGenerated, elapsedMs)
    }

    enum ChatError: Error {
        case notLoaded
    }
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
        router.post("/v1/chat") { request, _ async throws -> ChatResponse in
            let body = try await request.decode(as: ChatRequest.self, context: JSONDecoder())

            let (text, tokens, timeMs) = try await chatManager.respond(to: body.prompt)

            return ChatResponse(
                text: text,
                tokensGenerated: tokens,
                generationTimeMs: timeMs
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
