import Foundation
import Hummingbird

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

struct LlamaConfig: Sendable {
    let model: String
    let context: Int
    let tokens: Int
    let ngl: Int
    let temperature: Double
    let topP: Double
    let topK: Int
    let systemPrompt: String
    let maxTokensLimit: Int

    init(env: [String: String]) {
        model = env["QWEN_MODEL"] ?? "Qwen/Qwen3-4B-GGUF:Q4_K_M"
        context = readEnvInt("QWEN_CTX", default: 2048, env: env)
        tokens = readEnvInt("QWEN_TOKENS", default: 256, env: env)
        ngl = readEnvInt("QWEN_NGL", default: 99, env: env)
        temperature = readEnvDouble("QWEN_TEMP", default: 0.6, env: env)
        topP = readEnvDouble("QWEN_TOP_P", default: 0.95, env: env)
        topK = readEnvInt("QWEN_TOP_K", default: 20, env: env)
        systemPrompt = env["QWEN_SYSTEM"] ?? "You are a concise assistant for Jetson Orin Nano. /no_think"
        maxTokensLimit = readEnvInt("QWEN_MAX_TOKENS", default: 1024, env: env)
    }
}

actor LlamaRunner {
    enum RunnerError: Error {
        case missingBinary(String)
        case executionFailed(String)
    }

    private let config: LlamaConfig
    private let llamaPath: String
    private let env: [String: String]

    init(config: LlamaConfig, llamaPath: String, env: [String: String]) throws {
        self.config = config
        self.llamaPath = llamaPath
        self.env = env

        if !FileManager.default.fileExists(atPath: llamaPath) {
            throw RunnerError.missingBinary("Missing llama-cli at \(llamaPath)")
        }
    }

    func generate(for request: ChatRequest) async throws -> ChatResponse {
        let prompt = buildPrompt(system: request.system, messages: request.messages)
        let tokens = clamp(request.maxTokens ?? config.tokens, min: 1, max: config.maxTokensLimit)
        let temperature = clamp(request.temperature ?? config.temperature, min: 0.0, max: 2.0)
        let topP = clamp(request.topP ?? config.topP, min: 0.0, max: 1.0)
        let topK = clamp(request.topK ?? config.topK, min: 0, max: 200)

        let args = buildArgs(
            model: config.model,
            context: config.context,
            tokens: tokens,
            ngl: config.ngl,
            temperature: temperature,
            topP: topP,
            topK: topK,
            prompt: prompt
        )

        let start = DispatchTime.now()
        let output = try await withCheckedThrowingContinuation { continuation in
            let llamaPath = self.llamaPath
            let env = self.env
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let text = try runLlama(path: llamaPath, args: args, env: env)
                    continuation.resume(returning: text)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        let end = DispatchTime.now()
        let elapsedMs = Int64((end.uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000)

        return ChatResponse(reply: output, model: config.model, durationMs: elapsedMs)
    }

    private func buildPrompt(system: String?, messages: [ChatMessage]) -> String {
        var sections: [String] = []
        let systemPrompt = (system ?? config.systemPrompt).trimmingCharacters(in: .whitespacesAndNewlines)
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
}

@main
struct Qwen3ChatServer {
    static func main() async throws {
        let env = ProcessInfo.processInfo.environment
        let hostname = env["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let config = LlamaConfig(env: env)
        let port = readEnvInt("PORT", default: {{.PORT}}, env: env)
        let llamaPath = env["LLAMA_CLI_PATH"] ?? "/opt/llama/llama-cli"

        let frontendDist = resolveFrontendDist(env: env)
        print("Serving frontend from: \(frontendDist)")
        print("Using model: \(config.model)")

        let runner = try LlamaRunner(config: config, llamaPath: llamaPath, env: env)

        let router = Router()

        router.get("/health") { _, _ -> HealthResponse in
            HealthResponse(status: "ok", model: config.model)
        }

        router.post("/api/chat") { request, _ async throws -> ChatResponse in
            let body = try await request.decode(as: ChatRequest.self, context: JSONDecoder())
            return try await runner.generate(for: body)
        }

        router.add(middleware: FileMiddleware(frontendDist, searchForIndexHtml: true))

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: port))
        )

        print("Server running on http://\(hostname):\(port)")
        try await app.runService()
    }
}

func resolveFrontendDist(env: [String: String]) -> String {
    let envPath = env["FRONTEND_DIST"]
    let containerPath = "/app/frontend/dist"
    let cwdPath = FileManager.default.currentDirectoryPath + "/../frontend/dist"

    if let envPath {
        return envPath
    }

    if FileManager.default.fileExists(atPath: containerPath + "/index.html") {
        return containerPath
    }

    return cwdPath
}

func readEnvInt(_ key: String, default value: Int, env: [String: String]) -> Int {
    if let raw = env[key], let parsed = Int(raw) {
        return parsed
    }
    return value
}

func readEnvDouble(_ key: String, default value: Double, env: [String: String]) -> Double {
    if let raw = env[key], let parsed = Double(raw) {
        return parsed
    }
    return value
}

func clamp<T: Comparable>(_ value: T, min lower: T, max upper: T) -> T {
    return min(max(value, lower), upper)
}

func runLlama(path: String, args: [String], env: [String: String]) throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = args
    process.environment = env

    let stdoutPipe = Pipe()
    let stderrPipe = Pipe()
    process.standardOutput = stdoutPipe
    process.standardError = stderrPipe

    try process.run()

    let outputData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
    let errorData = stderrPipe.fileHandleForReading.readDataToEndOfFile()

    process.waitUntilExit()

    if process.terminationStatus != 0 {
        let errorText = String(decoding: errorData, as: UTF8.self)
        throw LlamaRunner.RunnerError.executionFailed(errorText.isEmpty ? "llama-cli failed" : errorText)
    }

    let output = String(decoding: outputData, as: UTF8.self)
    return output.trimmingCharacters(in: .whitespacesAndNewlines)
}

func buildArgs(
    model: String,
    context: Int,
    tokens: Int,
    ngl: Int,
    temperature: Double,
    topP: Double,
    topK: Int,
    prompt: String
) -> [String] {
    var args: [String] = []
    args += ["-hf", model]
    args += ["--jinja"]
    args += ["--no-display-prompt"]
    args += ["--log-disable"]
    args += ["-ngl", String(ngl)]
    args += ["-c", String(context)]
    args += ["-n", String(tokens)]
    args += ["--temp", String(temperature)]
    args += ["--top-p", String(topP)]
    args += ["--top-k", String(topK)]
    args += ["-p", prompt]
    return args
}
