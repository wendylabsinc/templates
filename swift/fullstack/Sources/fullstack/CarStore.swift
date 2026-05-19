internal import Foundation
import GRDB

struct Car: Codable, Sendable {
    let id: Int
    let make: String
    let model: String
    let color: String
    let year: Int
    let created_at: String?
    let updated_at: String?
}

extension Car: FetchableRecord, TableRecord {
    static let databaseTableName = "cars"
}

struct CarInput: Decodable, Sendable {
    let make: String
    let model: String
    let color: String
    let year: Int
}

actor CarStore {
    private let dbQueue: DatabaseQueue

    init(path: String = "/data/cars.db") throws {
        let dir = URL(filePath: path).deletingLastPathComponent().path()
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        self.dbQueue = try DatabaseQueue(path: path)
        try dbQueue.write { db in
            try db.create(table: "cars", ifNotExists: true) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("make", .text).notNull()
                t.column("model", .text).notNull()
                t.column("color", .text).notNull()
                t.column("year", .integer).notNull()
                t.column("created_at", .text).notNull().defaults(sql: "datetime('now')")
                t.column("updated_at", .text)
            }
        }
    }

    func all() throws -> [Car] {
        try dbQueue.read { db in
            try Car.order(Column("id")).fetchAll(db)
        }
    }

    func get(id: Int) throws -> Car? {
        try dbQueue.read { db in
            try Car.filter(Column("id") == id).fetchOne(db)
        }
    }

    func create(input: CarInput) throws -> Car? {
        try dbQueue.write { db in
            try db.execute(
                sql: "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
                arguments: [input.make, input.model, input.color, input.year]
            )
            return try Car.filter(Column("id") == db.lastInsertedRowID).fetchOne(db)
        }
    }

    func update(id: Int, input: CarInput) throws -> Car? {
        try dbQueue.write { db in
            try db.execute(
                sql: "UPDATE cars SET make=?, model=?, color=?, year=?, updated_at=datetime('now') WHERE id=?",
                arguments: [input.make, input.model, input.color, input.year, id]
            )
            return try Car.filter(Column("id") == id).fetchOne(db)
        }
    }

    func delete(id: Int) throws -> Bool {
        try dbQueue.write { db in
            try Car.filter(Column("id") == id).deleteAll(db) > 0
        }
    }
}
