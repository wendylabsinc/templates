/// Persistent Volume Example
///
/// Demonstrates reading and writing to a persistent volume that survives container restarts.

import Foundation

let dataDir = "/data"

@main
struct PersistentVolume {
    static func main() throws {
        print("Persistent Volume Example")
        print(String(repeating: "=", count: 40))

        let fileManager = FileManager.default
        let dataURL = URL(fileURLWithPath: dataDir)

        // Ensure the data directory exists
        try fileManager.createDirectory(at: dataURL, withIntermediateDirectories: true)

        // Create and write to foo.md
        let fooURL = dataURL.appendingPathComponent("foo.md")
        let content = """
            # Hello from Persistent Storage

            This file was created by the persistent-volume example.

            It will survive container restarts because it's stored
            in a persistent volume mounted at `/data`.

            """

        print("\nWriting to \(fooURL.path)...")
        try content.write(to: fooURL, atomically: true, encoding: .utf8)
        print("Done!")

        // Read and display foo.md
        print("\nReading from \(fooURL.path):")
        print(String(repeating: "-", count: 40))
        let readContent = try String(contentsOf: fooURL, encoding: .utf8)
        print(readContent, terminator: "")
        print(String(repeating: "-", count: 40))

        // List all items in the persistent volume
        print("\nContents of \(dataDir):")
        let items = try fileManager.contentsOfDirectory(at: dataURL, includingPropertiesForKeys: [.isDirectoryKey])
        for item in items {
            let isDirectory = (try? item.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
            let itemType = isDirectory ? "DIR " : "FILE"
            print("  [\(itemType)] \(item.lastPathComponent)")
        }

        print("\nPersistent volume example complete!")
    }
}
