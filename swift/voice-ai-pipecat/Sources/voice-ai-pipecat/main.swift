import Foundation
import PythonKit

// Swift entrypoint for the voice-ai-pipecat template.
//
// This is a thin bridge: the real pipeline logic lives in `main.py` /
// `pipeline.py` and is imported via PythonKit. Swift owns process startup,
// environment wiring, and graceful shutdown; Pipecat owns the audio pipeline.

@main
struct VoiceAIPipecat {
    static func main() {
        let sys = Python.import("sys")
        sys.path.append("/app/python")

        let port = ProcessInfo.processInfo.environment["PORT"] ?? "6005"
        setenv("PORT", port, 1)

        let mainModule = Python.import("main")
        print("Starting voice-ai-pipecat on port \(port)")
        mainModule.main()
    }
}
