/// SQLite Persistence Example
///
/// Demonstrates using SQLite with a persistent volume that survives container restarts.

import Foundation
import SQLite

let dataDir = "/data"

@main
struct SQLitePersistence {
    static func main() throws {
        print("SQLite Persistence Example")
        print(String(repeating: "=", count: 40))

        let fileManager = FileManager.default
        let dataURL = URL(fileURLWithPath: dataDir)
        let dbPath = dataURL.appendingPathComponent("app.db").path

        // Ensure the data directory exists
        try fileManager.createDirectory(at: dataURL, withIntermediateDirectories: true)

        // Connect to SQLite database
        print("\nConnecting to database: \(dbPath)")
        let db = try Connection(dbPath)

        // Define table schema
        let runs = Table("runs")
        let id = SQLite.Expression<Int64>("id")
        let timestamp = SQLite.Expression<String>("timestamp")
        let message = SQLite.Expression<String>("message")

        // Create table if it doesn't exist
        try db.run(runs.create(ifNotExists: true) { t in
            t.column(id, primaryKey: .autoincrement)
            t.column(timestamp)
            t.column(message)
        })

        // Insert a new record for this run
        let now = ISO8601DateFormatter().string(from: Date())
        let msg = "Application started at \(now)"
        try db.run(runs.insert(timestamp <- now, message <- msg))
        print("Inserted: \(msg)")

        // Query all records
        print("\n" + String(repeating: "-", count: 40))
        print("All recorded runs:")
        print(String(repeating: "-", count: 40))

        var count = 0
        for run in try db.prepare(runs.order(id)) {
            print("  [\(run[id])] \(run[timestamp]): \(run[message])")
            count += 1
        }

        print("\nTotal runs: \(count)")
        print(String(repeating: "-", count: 40))

        print("\nSQLite persistence example complete!")
        print("Run this again to see the data persist across restarts.")
    }
}
