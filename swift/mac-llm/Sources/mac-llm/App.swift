import ArgumentParser
import Dispatch
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

struct CLIOptions: ParsableCommand, Sendable {
    static let configuration = CommandConfiguration(commandName: "{{.APP_ID}}")

    @Option(name: .long, help: "Interface to bind Open WebUI to. Use 0.0.0.0 for LAN access.")
    var webuiHost: String = "0.0.0.0"

    @Option(name: .long, help: "Port for Open WebUI.")
    var webuiPort: Int = {{.PORT}}

    @Option(name: .long, help: "Interface to bind the private MLX OpenAI-compatible API to.")
    var mlxHost: String = "127.0.0.1"

    @Option(name: .long, help: "Port for the private MLX OpenAI-compatible API.")
    var mlxPort: Int = {{.MLX_PORT}}

    @Option(name: .long, help: "Hugging Face MLX model ID.")
    var model: String = "{{.MODEL_ID}}"

    @Option(name: .customLong("open-webui-version"), help: "Pinned Open WebUI Python package version to install with uv.")
    var openWebUIVersion: String = "{{.OPEN_WEBUI_VERSION}}"

    @Option(name: .long, help: "Default maximum generated tokens when requests omit max_tokens.")
    var defaultMaxTokens: Int = {{.MAX_TOKENS}}

    func validate() throws {
        guard (1...65535).contains(webuiPort) else {
            throw ValidationError("--webui-port must be between 1 and 65535.")
        }
        guard (1...65535).contains(mlxPort) else {
            throw ValidationError("--mlx-port must be between 1 and 65535.")
        }
        guard webuiPort != mlxPort || webuiHost != mlxHost else {
            throw ValidationError("--webui-port and --mlx-port must not collide on the same interface.")
        }
        guard defaultMaxTokens > 0 else {
            throw ValidationError("--default-max-tokens must be greater than 0.")
        }
        guard !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ValidationError("--model must not be empty.")
        }
        guard !openWebUIVersion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ValidationError("--open-webui-version must not be empty.")
        }
    }
}

@main
struct MacMLXLLMApp {
    static func main() async throws {
        let options: CLIOptions
        do {
            options = try CLIOptions.parse(filteredCommandLineArguments())
        } catch {
            CLIOptions.exit(withError: error)
        }

        let runtime = try RuntimeLayout(appID: "{{.APP_ID}}")
        try runtime.createDirectories()
        runtime.applyEnvironmentToCurrentProcess()

        let apiKey = try runtime.loadOrCreateAPIKey()
        let openWebUI = OpenWebUISupervisor(options: options, runtime: runtime, apiKey: apiKey)
        let service = MLXLLMService(modelID: options.model, defaultMaxTokens: options.defaultMaxTokens)

        try openWebUI.ensureInstalled()
        try await service.prepare()

        let openWebUIProcess = try openWebUI.start()
        let shutdownHandlers = installShutdownHandlers(openWebUIProcess: openWebUIProcess)
        defer {
            shutdownHandlers.forEach { $0.cancel() }
            openWebUIProcess.terminate()
        }

        let router = buildRouter(options: options, service: service, apiKey: apiKey)
        let app = Application(
            router: router,
            configuration: .init(address: .hostname(options.mlxHost, port: options.mlxPort))
        )

        print("MLX_MODEL=\(options.model)")
        print("MLX_OPENAI_BASE_URL=http://\(options.mlxHost):\(options.mlxPort)/v1")
        print("OPEN_WEBUI_URL=http://\(ProcessInfo.processInfo.hostName):\(options.webuiPort)")
        print("OPEN_WEBUI_DATA_DIR=\(runtime.openWebUIDataURL.path)")
        print("HUGGINGFACE_HUB_CACHE=\(runtime.huggingFaceHubCacheURL.path)")
        print("The MLX API is bound to localhost and protected with an app-generated API key.")

        try await app.runService()
    }
}

func installShutdownHandlers(openWebUIProcess: RunningProcess) -> [DispatchSourceSignal] {
    let signals = [SIGTERM, SIGINT]
    return signals.map { signalNumber in
        signal(signalNumber, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
        source.setEventHandler {
            print("Received signal \(signalNumber); stopping Open WebUI.")
            openWebUIProcess.terminate()
            openWebUIProcess.waitUntilExit()
            Foundation.exit(signalNumber == SIGINT ? 130 : 143)
        }
        source.resume()
        return source
    }
}

func buildRouter(options: CLIOptions, service: MLXLLMService, apiKey: String) -> Router<BasicRequestContext> {
    let router = Router()

    router.get("/") { _, _ in
        try jsonResponse(APIStatus(
            message: "{{.APP_ID}} is running. Open WebUI is served by the companion process.",
            model: options.model,
            openWebUIURL: "http://<this-mac>:\(options.webuiPort)",
            openAIBaseURL: "http://\(options.mlxHost):\(options.mlxPort)/v1"
        ))
    }

    router.get("/health") { _, _ -> HTTPResponse.Status in
        .ok
    }

    router.get("/v1/models") { request, _ -> Response in
        if let unauthorized = try unauthorizedResponseIfNeeded(request: request, apiKey: apiKey) {
            return unauthorized
        }
        return try jsonResponse(ModelListResponse(data: [ModelInfo(id: options.model)]))
    }

    router.post("/v1/chat/completions") { request, context -> Response in
        if let unauthorized = try unauthorizedResponseIfNeeded(request: request, apiKey: apiKey) {
            return unauthorized
        }
        let completionRequest = try await request.decode(as: ChatCompletionRequest.self, context: context)
        if completionRequest.stream == true {
            return streamingResponse(service: service, request: completionRequest)
        }
        let completion = try await service.complete(request: completionRequest)
        return try jsonResponse(completion)
    }

    return router
}

struct RuntimeLayout: Sendable {
    let appID: String
    let rootURL: URL

    init(appID: String) throws {
        self.appID = appID
        let baseURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support", isDirectory: true)
        self.rootURL = baseURL.appendingPathComponent(appID, isDirectory: true)
    }

    var binURL: URL { rootURL.appendingPathComponent("bin", isDirectory: true) }
    var cacheURL: URL { rootURL.appendingPathComponent("cache", isDirectory: true) }
    var homeURL: URL { rootURL.appendingPathComponent("home", isDirectory: true) }
    var logsURL: URL { rootURL.appendingPathComponent("logs", isDirectory: true) }
    var secretsURL: URL { rootURL.appendingPathComponent("secrets", isDirectory: true) }
    var uvToolsURL: URL { rootURL.appendingPathComponent("uv-tools", isDirectory: true) }
    var uvToolBinURL: URL { binURL }
    var uvPythonURL: URL { rootURL.appendingPathComponent("uv-python", isDirectory: true) }
    var huggingFaceHomeURL: URL {
        if let value = ProcessInfo.processInfo.environment["HF_HOME"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache", isDirectory: true)
            .appendingPathComponent("huggingface", isDirectory: true)
    }
    var huggingFaceHubCacheURL: URL {
        if let value = ProcessInfo.processInfo.environment["HF_HUB_CACHE"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return huggingFaceHomeURL.appendingPathComponent("hub", isDirectory: true)
    }
    var openWebUIDataURL: URL { rootURL.appendingPathComponent("open-webui-data", isDirectory: true) }
    var openWebUIVersionURL: URL { rootURL.appendingPathComponent("open-webui.version") }
    var apiKeyURL: URL { secretsURL.appendingPathComponent("mlx-api-key") }
    var openWebUIExecutableURL: URL { uvToolBinURL.appendingPathComponent("open-webui") }

    func createDirectories() throws {
        for url in [rootURL, binURL, cacheURL, homeURL, logsURL, secretsURL, uvToolsURL, uvToolBinURL, uvPythonURL, huggingFaceHomeURL, huggingFaceHubCacheURL, openWebUIDataURL] {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        }
    }

    func applyEnvironmentToCurrentProcess() {
        setenv("HF_HOME", huggingFaceHomeURL.path, 1)
        setenv("HF_HUB_CACHE", huggingFaceHubCacheURL.path, 1)
        setenv("TOKENIZERS_PARALLELISM", "false", 1)
    }

    func loadOrCreateAPIKey() throws -> String {
        if let existing = try? String(contentsOf: apiKeyURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), !existing.isEmpty {
            return existing
        }

        let key = "wendy-mlx-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        try key.write(to: apiKeyURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: apiKeyURL.path)
        return key
    }

    func uvEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let currentPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = "\(binURL.path):/opt/homebrew/bin:/usr/local/bin:\(currentPath)"
        env["HOME"] = homeURL.path
        env["XDG_CACHE_HOME"] = cacheURL.path
        env["UV_CACHE_DIR"] = cacheURL.appendingPathComponent("uv", isDirectory: true).path
        env["UV_TOOL_DIR"] = uvToolsURL.path
        env["UV_TOOL_BIN_DIR"] = uvToolBinURL.path
        env["UV_PYTHON_INSTALL_DIR"] = uvPythonURL.path
        env["UV_PYTHON_PREFERENCE"] = "managed"
        return env
    }
}

struct OpenWebUISupervisor: Sendable {
    let options: CLIOptions
    let runtime: RuntimeLayout
    let apiKey: String

    func ensureInstalled() throws {
        let installedVersion = (try? String(contentsOf: runtime.openWebUIVersionURL, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        if FileManager.default.isExecutableFile(atPath: runtime.openWebUIExecutableURL.path), installedVersion == options.openWebUIVersion {
            print("Open WebUI \(options.openWebUIVersion) already installed at \(runtime.openWebUIExecutableURL.path)")
            return
        }

        let uv = try findUVExecutable()
        print("Installing Open WebUI \(options.openWebUIVersion) with uv. This may take a few minutes on first run...")
        try runCommand(
            executable: uv,
            arguments: ["python", "install", "3.11"],
            environment: runtime.uvEnvironment(),
            prefix: "uv"
        )
        try runCommand(
            executable: uv,
            arguments: ["tool", "install", "open-webui==\(options.openWebUIVersion)", "--python", "3.11", "--force"],
            environment: runtime.uvEnvironment(),
            prefix: "uv"
        )
        try options.openWebUIVersion.write(to: runtime.openWebUIVersionURL, atomically: true, encoding: .utf8)
    }

    func start() throws -> RunningProcess {
        guard FileManager.default.isExecutableFile(atPath: runtime.openWebUIExecutableURL.path) else {
            throw RuntimeError("open-webui executable was not found at \(runtime.openWebUIExecutableURL.path)")
        }

        var env = runtime.uvEnvironment()
        env["DATA_DIR"] = runtime.openWebUIDataURL.path
        env["WEBUI_AUTH"] = "True"
        env["ENABLE_PERSISTENT_CONFIG"] = "False"
        env["WEBUI_NAME"] = "Wendy MLX"
        env["HOST"] = options.webuiHost
        env["PORT"] = String(options.webuiPort)
        env["ENABLE_OLLAMA_API"] = "False"
        env["ENABLE_OPENAI_API"] = "True"
        env["OPENAI_API_BASE_URL"] = "http://\(options.mlxHost):\(options.mlxPort)/v1"
        env["OPENAI_API_BASE_URLS"] = "http://\(options.mlxHost):\(options.mlxPort)/v1"
        env["OPENAI_API_KEYS"] = apiKey
        env["HF_HOME"] = runtime.huggingFaceHomeURL.path
        env["HF_HUB_CACHE"] = runtime.huggingFaceHubCacheURL.path

        let process = Process()
        process.executableURL = runtime.openWebUIExecutableURL
        process.arguments = [
            "serve",
            "--host", options.webuiHost,
            "--port", String(options.webuiPort),
        ]
        process.environment = env
        process.currentDirectoryURL = runtime.openWebUIDataURL
        pipeProcessOutput(process, prefix: "open-webui")
        process.terminationHandler = { process in
            let status = process.terminationStatus
            if status == 0 {
                print("Open WebUI exited.")
            } else {
                print("Open WebUI exited with status \(status).")
                Foundation.exit(status)
            }
        }

        print("Starting Open WebUI on http://\(options.webuiHost):\(options.webuiPort)")
        try process.run()
        return RunningProcess(process: process)
    }

    private func findUVExecutable() throws -> String {
        let env = ProcessInfo.processInfo.environment
        var candidates: [String] = []
        if let configured = env["UV_BIN"], !configured.isEmpty {
            candidates.append(configured)
        }
        candidates.append("/opt/homebrew/bin/uv")
        candidates.append("/opt/homebrew/opt/uv/bin/uv")
        candidates.append("/usr/local/bin/uv")
        candidates.append("/usr/local/opt/uv/bin/uv")
        for directory in (env["PATH"] ?? "").split(separator: ":") {
            candidates.append(String(directory) + "/uv")
        }

        if let brew = findExecutable(named: "brew"), let uvPrefix = try? captureCommand(executable: brew, arguments: ["--prefix", "uv"]) {
            candidates.append(URL(fileURLWithPath: uvPrefix).appendingPathComponent("bin/uv").path)
        }
        if let brew = findExecutable(named: "brew"), let brewPrefix = try? captureCommand(executable: brew, arguments: ["--prefix"]) {
            candidates.append(URL(fileURLWithPath: brewPrefix).appendingPathComponent("bin/uv").path)
        }

        if let uv = firstExecutable(in: candidates) {
            return uv
        }

        if let brew = findExecutable(named: "brew") {
            print("uv was not found after Brewfile application; running `brew install uv` as a fallback...")
            try? runCommand(
                executable: brew,
                arguments: ["install", "uv"],
                environment: ProcessInfo.processInfo.environment,
                prefix: "brew"
            )
            if let uvPrefix = try? captureCommand(executable: brew, arguments: ["--prefix", "uv"]) {
                candidates.append(URL(fileURLWithPath: uvPrefix).appendingPathComponent("bin/uv").path)
            }
            if let brewPrefix = try? captureCommand(executable: brew, arguments: ["--prefix"]) {
                candidates.append(URL(fileURLWithPath: brewPrefix).appendingPathComponent("bin/uv").path)
            }
            candidates += cellarExecutables(formula: "uv", executable: "uv")
            if let uv = firstExecutable(in: candidates) {
                return uv
            }
        }

        throw RuntimeError("uv is required but was not found. Checked: \(candidates.joined(separator: ", ")). Wendy should install it from Brewfile.wendy before starting this app; verify Homebrew is installed on the target Mac and `brew bundle` succeeded.")
    }
}

final class RunningProcess: @unchecked Sendable {
    private let process: Process

    init(process: Process) {
        self.process = process
    }

    func terminate() {
        if process.isRunning {
            process.terminate()
        }
    }

    func waitUntilExit() {
        process.waitUntilExit()
    }
}

struct RuntimeError: Error, CustomStringConvertible {
    let description: String

    init(_ description: String) {
        self.description = description
    }
}

func findExecutable(named name: String) -> String? {
    let env = ProcessInfo.processInfo.environment
    var candidates = ["/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)"]
    for directory in (env["PATH"] ?? "").split(separator: ":") {
        candidates.append(String(directory) + "/\(name)")
    }

    var seen = Set<String>()
    for candidate in candidates where seen.insert(candidate).inserted {
        if FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
    }
    return nil
}

func firstExecutable(in candidates: [String]) -> String? {
    var seen = Set<String>()
    for candidate in candidates where seen.insert(candidate).inserted {
        if FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
    }
    return nil
}

func cellarExecutables(formula: String, executable: String) -> [String] {
    var results: [String] = []
    for root in ["/opt/homebrew/Cellar", "/usr/local/Cellar"] {
        let formulaURL = URL(fileURLWithPath: root).appendingPathComponent(formula, isDirectory: true)
        guard let versions = try? FileManager.default.contentsOfDirectory(
            at: formulaURL,
            includingPropertiesForKeys: nil
        ) else { continue }
        for version in versions {
            results.append(version.appendingPathComponent("bin/\(executable)").path)
        }
    }
    return results
}

func captureCommand(executable: String, arguments: [String]) throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments

    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe

    try process.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()

    guard process.terminationStatus == 0 else {
        throw RuntimeError("\(executable) \(arguments.joined(separator: " ")) exited with status \(process.terminationStatus)")
    }
    return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

func runCommand(executable: String, arguments: [String], environment: [String: String], prefix: String) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.environment = environment
    pipeProcessOutput(process, prefix: prefix)
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        throw RuntimeError("\(executable) \(arguments.joined(separator: " ")) exited with status \(process.terminationStatus)")
    }
}

func pipeProcessOutput(_ process: Process, prefix: String) {
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe
    let handle = pipe.fileHandleForReading
    Thread.detachNewThread {
        while true {
            let data = handle.availableData
            if data.isEmpty { break }
            guard let text = String(data: data, encoding: .utf8) else { continue }
            for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
                if !line.isEmpty {
                    print("[\(prefix)] \(line)")
                }
            }
        }
    }
}

func unauthorizedResponseIfNeeded(request: Request, apiKey: String) throws -> Response? {
    let expected = "Bearer \(apiKey)"
    guard request.headers[.authorization] == expected else {
        return try jsonResponse(
            ErrorResponse(error: ErrorBody(message: "missing or invalid bearer token", type: "authentication_error")),
            status: .unauthorized,
            headers: [.wwwAuthenticate: "Bearer"]
        )
    }
    return nil
}

final class ModelDownloadProgressLogger: @unchecked Sendable {
    private let modelID: String
    private let lock = NSLock()
    private var lastPercent = -1
    private var lastReportedAt = Date.distantPast

    init(modelID: String) {
        self.modelID = modelID
    }

    func log(_ progress: Progress) {
        let total = progress.totalUnitCount
        let completed = progress.completedUnitCount
        let now = Date()
        let message: String?

        lock.lock()
        defer { lock.unlock() }

        if total > 0 {
            let clampedCompleted = min(max(completed, 0), total)
            let percent = min(100, max(0, Int((Double(clampedCompleted) / Double(total) * 100).rounded(.down))))
            let elapsed = now.timeIntervalSince(lastReportedAt)
            guard percent > lastPercent || elapsed >= 10 else {
                return
            }

            lastPercent = max(lastPercent, percent)
            lastReportedAt = now
            let completedText = ByteCountFormatter.string(fromByteCount: clampedCompleted, countStyle: .file)
            let totalText = ByteCountFormatter.string(fromByteCount: total, countStyle: .file)
            message = "Hugging Face download \(modelID): \(lastPercent)% (\(completedText) / \(totalText))"
        } else {
            let elapsed = now.timeIntervalSince(lastReportedAt)
            guard elapsed >= 10 else {
                return
            }

            lastReportedAt = now
            let completedText = ByteCountFormatter.string(fromByteCount: max(completed, 0), countStyle: .file)
            message = "Hugging Face download \(modelID): \(completedText) downloaded"
        }

        if let message {
            print(message)
            fflush(stdout)
        }
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

    func prepare() async throws {
        _ = try await loadContainerIfNeeded()
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

        print("Preparing MLX model \(modelID). The first run may download weights from Hugging Face before Open WebUI is marked ready...")
        let progressLogger = ModelDownloadProgressLogger(modelID: modelID)
        let loaded = try await #huggingFaceLoadModelContainer(
            configuration: ModelConfiguration(id: modelID),
            progressHandler: { progress in
                progressLogger.log(progress)
            }
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
    let openWebUIURL: String
    let openAIBaseURL: String

    enum CodingKeys: String, CodingKey {
        case message
        case model
        case openWebUIURL = "open_webui_url"
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
        headers: [.contentType: "text/event-stream; charset=utf-8"],
        body: ResponseBody(asyncSequence: stream)
    )
}

func jsonResponse<T: Encodable>(_ value: T, status: HTTPResponse.Status = .ok, headers extraHeaders: HTTPFields = [:]) throws -> Response {
    let data = try JSONEncoder().encode(value)
    var headers: HTTPFields = [.contentType: "application/json; charset=utf-8"]
    for field in extraHeaders {
        headers.append(field)
    }
    return Response(
        status: status,
        headers: headers,
        body: ResponseBody(byteBuffer: ByteBuffer(bytes: data))
    )
}
