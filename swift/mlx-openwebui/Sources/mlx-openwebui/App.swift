import ArgumentParser
import Foundation
import Hummingbird
import HuggingFace
import MLXHuggingFace
import MLXLLM
import MLXLMCommon
import Tokenizers

func filteredCommandLineArguments() -> [String] {
    let rawArguments = Array(CommandLine.arguments.dropFirst())
    var filtered: [String] = []
    var skipNext = false

    for argument in rawArguments {
        if skipNext {
            skipNext = false
            continue
        }
        if argument.hasPrefix("-NS") || argument.hasPrefix("-Apple") {
            skipNext = true
            continue
        }
        filtered.append(argument)
    }

    return filtered
}

struct CLIOptions: ParsableCommand {
    static let configuration = CommandConfiguration(commandName: "{{.APP_ID}}")

    @Option(name: .long, help: "Interface to bind the OpenAI-compatible HTTP API to.")
    var host: String = "0.0.0.0"

    @Option(name: .long, help: "Port to serve the OpenAI-compatible HTTP API on.")
    var port: Int = {{.PORT}}

    @Option(name: .long, help: "Hugging Face MLX model ID.")
    var model: String = "{{.MODEL_ID}}"

    @Option(name: .long, help: "Default maximum generated tokens when requests omit max_tokens.")
    var defaultMaxTokens: Int = {{.MAX_TOKENS}}

    func validate() throws {
        guard (1...65535).contains(port) else {
            throw ValidationError("--port must be between 1 and 65535.")
        }
        guard defaultMaxTokens > 0 else {
            throw ValidationError("--default-max-tokens must be greater than 0.")
        }
        guard !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ValidationError("--model must not be empty.")
        }
    }
}

@main
struct MLXOpenWebUIBackend {
    static func main() async throws {
        let options = try CLIOptions.parse(filteredCommandLineArguments())
        let service = MLXLLMService(modelID: options.model, defaultMaxTokens: options.defaultMaxTokens)
        let router = Router()

        router.get("/") { _, _ in
            try jsonResponse(APIStatus(
                message: "{{.APP_ID}} is running. Configure Open WebUI with the /v1 base URL.",
                model: options.model,
                openAIBaseURL: "http://<this-mac>:\(options.port)/v1"
            ))
        }

        router.get("/health") { _, _ -> HTTPResponse.Status in
            .ok
        }

        router.get("/v1/models") { _, _ in
            try jsonResponse(ModelListResponse(data: [ModelInfo(id: options.model)]))
        }

        router.post("/v1/chat/completions") { request, context in
            let completionRequest = try await request.decode(as: ChatCompletionRequest.self, context: context)
            if completionRequest.stream == true {
                return streamingResponse(service: service, request: completionRequest)
            }
            let completion = try await service.complete(request: completionRequest)
            return try jsonResponse(completion)
        }

        let app = Application(
            router: router,
            configuration: .init(address: .hostname(options.host, port: options.port))
        )

        print("MLX_OPENWEBUI_MODEL=\(options.model)")
        print("MLX_OPENWEBUI_BASE_URL=http://\(ProcessInfo.processInfo.hostName):\(options.port)/v1")
        print("Open WebUI API key: any non-empty value")
        try await app.runService()
    }
}

actor MLXLLMService {
    private let modelID: String
    private let defaultMaxTokens: Int
    private var container: ModelContainer?

    init(modelID: String, defaultMaxTokens: Int) {
        self.modelID = modelID
        self.defaultMaxTokens = defaultMaxTokens
    }

    func complete(request: ChatCompletionRequest) async throws -> ChatCompletionResponse {
        let container = try await loadContainerIfNeeded()
        let messages = request.messages.map(\.chatMessage)
        let input = try await container.prepare(input: UserInput(chat: messages))
        let stream = try await container.generate(input: input, parameters: parameters(for: request))

        var content = ""
        var usage = Usage(promptTokens: 0, completionTokens: 0, totalTokens: 0)
        for await generation in stream {
            switch generation {
            case .chunk(let text):
                content += text
            case .info(let info):
                usage = Usage(
                    promptTokens: info.promptTokenCount,
                    completionTokens: info.generationTokenCount,
                    totalTokens: info.promptTokenCount + info.generationTokenCount
                )
            case .toolCall:
                break
            }
        }

        return ChatCompletionResponse(
            id: Self.makeCompletionID(),
            created: Self.createdTimestamp(),
            model: modelID,
            choices: [
                ChatCompletionChoice(
                    index: 0,
                    message: AssistantMessage(role: "assistant", content: content),
                    finishReason: "stop"
                )
            ],
            usage: usage
        )
    }

    private func loadContainerIfNeeded() async throws -> ModelContainer {
        if let container {
            return container
        }

        print("Loading MLX model \(modelID). The first run may download weights from Hugging Face...")
        let loaded = try await #huggingFaceLoadModelContainer(
            configuration: ModelConfiguration(id: modelID)
        )
        container = loaded
        print("MLX model ready: \(modelID)")
        return loaded
    }

    private func parameters(for request: ChatCompletionRequest) -> GenerateParameters {
        GenerateParameters(
            maxTokens: request.maxTokens ?? defaultMaxTokens,
            temperature: Float(request.temperature ?? 0.7),
            topP: Float(request.topP ?? 1.0)
        )
    }

    private static func makeCompletionID() -> String {
        "chatcmpl-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    private static func createdTimestamp() -> Int {
        Int(Date().timeIntervalSince1970)
    }
}

struct ChatCompletionRequest: Decodable, Sendable {
    let model: String?
    let messages: [OpenAIChatMessage]
    let stream: Bool?
    let maxTokens: Int?
    let temperature: Double?
    let topP: Double?

    enum CodingKeys: String, CodingKey {
        case model
        case messages
        case stream
        case maxTokens = "max_tokens"
        case temperature
        case topP = "top_p"
    }
}

struct OpenAIChatMessage: Decodable, Sendable {
    let role: String
    let content: MessageContent?

    var chatMessage: Chat.Message {
        let text = content?.plainText ?? ""
        switch role.lowercased() {
        case "system":
            return .system(text)
        case "assistant":
            return .assistant(text)
        case "tool":
            return .tool(text)
        default:
            return .user(text)
        }
    }
}

enum MessageContent: Decodable, Sendable {
    case text(String)
    case parts([ContentPart])

    var plainText: String {
        switch self {
        case .text(let text):
            return text
        case .parts(let parts):
            return parts.compactMap(\.text).joined(separator: "\n")
        }
    }

    init(from decoder: Swift.Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let text = try? container.decode(String.self) {
            self = .text(text)
            return
        }
        self = .parts(try container.decode([ContentPart].self))
    }
}

struct ContentPart: Decodable, Sendable {
    let type: String?
    let text: String?
}

struct APIStatus: Encodable {
    let message: String
    let model: String
    let openAIBaseURL: String

    enum CodingKeys: String, CodingKey {
        case message
        case model
        case openAIBaseURL = "openai_base_url"
    }
}

struct ModelListResponse: Encodable {
    let object = "list"
    let data: [ModelInfo]
}

struct ModelInfo: Encodable {
    let id: String
    let object = "model"
    let created = 0
    let ownedBy = "local-mlx"

    enum CodingKeys: String, CodingKey {
        case id
        case object
        case created
        case ownedBy = "owned_by"
    }
}

struct ChatCompletionResponse: Encodable {
    let id: String
    let object = "chat.completion"
    let created: Int
    let model: String
    let choices: [ChatCompletionChoice]
    let usage: Usage
}

struct ChatCompletionChoice: Encodable {
    let index: Int
    let message: AssistantMessage
    let finishReason: String

    enum CodingKeys: String, CodingKey {
        case index
        case message
        case finishReason = "finish_reason"
    }
}

struct AssistantMessage: Encodable {
    let role: String
    let content: String
}

struct Usage: Encodable {
    let promptTokens: Int
    let completionTokens: Int
    let totalTokens: Int

    enum CodingKeys: String, CodingKey {
        case promptTokens = "prompt_tokens"
        case completionTokens = "completion_tokens"
        case totalTokens = "total_tokens"
    }
}

struct ChatCompletionStreamChunk: Encodable {
    let id: String
    let object = "chat.completion.chunk"
    let created: Int
    let model: String
    let choices: [StreamChoice]
}

struct StreamChoice: Encodable {
    let index: Int
    let delta: StreamDelta
    let finishReason: String?

    enum CodingKeys: String, CodingKey {
        case index
        case delta
        case finishReason = "finish_reason"
    }
}

struct StreamDelta: Encodable {
    let role: String?
    let content: String?
}

struct ErrorResponse: Encodable {
    let error: ErrorBody
}

struct ErrorBody: Encodable {
    let message: String
    let type: String
}

func streamingResponse(service: MLXLLMService, request: ChatCompletionRequest) -> Response {
    let id = "chatcmpl-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    let created = Int(Date().timeIntervalSince1970)
    let model = request.model ?? "mlx"
    let encoder = JSONEncoder()

    let stream = AsyncStream<ByteBuffer> { continuation in
        Task {
            do {
                func yieldEvent(_ chunk: ChatCompletionStreamChunk) throws {
                    let data = try encoder.encode(chunk)
                    if let json = String(data: data, encoding: .utf8) {
                        continuation.yield(ByteBuffer(string: "data: \(json)\n\n"))
                    }
                }

                try yieldEvent(ChatCompletionStreamChunk(
                    id: id,
                    created: created,
                    model: model,
                    choices: [StreamChoice(index: 0, delta: StreamDelta(role: "assistant", content: nil), finishReason: nil)]
                ))

                let completion = try await service.complete(request: request)
                if let text = completion.choices.first?.message.content, !text.isEmpty {
                    try yieldEvent(ChatCompletionStreamChunk(
                        id: id,
                        created: created,
                        model: model,
                        choices: [StreamChoice(index: 0, delta: StreamDelta(role: nil, content: text), finishReason: nil)]
                    ))
                }

                try yieldEvent(ChatCompletionStreamChunk(
                    id: id,
                    created: created,
                    model: model,
                    choices: [StreamChoice(index: 0, delta: StreamDelta(role: nil, content: nil), finishReason: "stop")]
                ))
                continuation.yield(ByteBuffer(string: "data: [DONE]\n\n"))
                continuation.finish()
            } catch {
                let payload = ErrorResponse(error: ErrorBody(message: String(describing: error), type: "server_error"))
                if let data = try? encoder.encode(payload), let json = String(data: data, encoding: .utf8) {
                    continuation.yield(ByteBuffer(string: "data: \(json)\n\n"))
                }
                continuation.yield(ByteBuffer(string: "data: [DONE]\n\n"))
                continuation.finish()
            }
        }
    }

    return Response(
        status: .ok,
        headers: [
            .contentType: "text/event-stream; charset=utf-8"
        ],
        body: ResponseBody(asyncSequence: stream)
    )
}

func jsonResponse<T: Encodable>(_ value: T, status: HTTPResponse.Status = .ok) throws -> Response {
    let data = try JSONEncoder().encode(value)
    return Response(
        status: status,
        headers: [.contentType: "application/json; charset=utf-8"],
        body: ResponseBody(byteBuffer: ByteBuffer(bytes: data))
    )
}
