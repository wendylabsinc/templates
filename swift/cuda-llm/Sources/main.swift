import MLXLLM
import MLXLMCommon
import MLXHuggingFace
import HuggingFace
import Tokenizers
import Foundation
import MLX

try! await Stream.withNewDefaultStream(device: .gpu) {
    let model: ModelContainer = try await #huggingFaceLoadModelContainer(
        configuration: LLMRegistry.gemma3_1B_qat_4bit
    )
    let session = ChatSession(model)
    print(try await session.respond(to: "What are two things to see in San Francisco?"))
    print(try await session.respond(to: "How about a great place to eat?"))
}
