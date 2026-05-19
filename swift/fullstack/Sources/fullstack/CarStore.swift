struct Car: Codable, Sendable {
    let id: Int
    let make: String
    let model: String
    let color: String
    let year: Int
    let created_at: String?
    let updated_at: String?
}

struct CarInput: Decodable, Sendable {
    let make: String
    let model: String
    let color: String
    let year: Int
}

actor CarStore {
    private let db: SQLiteDB

    init(path: String = "/data/cars.db") throws {
        self.db = try SQLiteDB(path: path)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                make TEXT NOT NULL,
                model TEXT NOT NULL,
                color TEXT NOT NULL,
                year INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT
            )
        """)
    }

    func all() throws -> [Car] {
        try db.exec("SELECT * FROM cars ORDER BY id").compactMap(carFromRow)
    }

    func get(id: Int) throws -> Car? {
        try db.exec("SELECT * FROM cars WHERE id = \(id)").first.flatMap(carFromRow)
    }

    func create(input: CarInput) throws -> Car? {
        let rowId = try db.run(
            "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
            bindings: [input.make, input.model, input.color, input.year]
        )
        return try db.exec("SELECT * FROM cars WHERE id = \(rowId)").first.flatMap(carFromRow)
    }

    func update(id: Int, input: CarInput) throws -> Car? {
        try db.run(
            "UPDATE cars SET make=?, model=?, color=?, year=?, updated_at=datetime('now') WHERE id=?",
            bindings: [input.make, input.model, input.color, input.year, id]
        )
        return try get(id: id)
    }

    func delete(id: Int) throws -> Bool {
        try db.run("DELETE FROM cars WHERE id = ?", bindings: [id])
        return db.changes > 0
    }

    private func carFromRow(_ row: [String: String?]) -> Car? {
        guard
            let idStr = row["id"] ?? nil, let id = Int(idStr),
            let make = row["make"] ?? nil,
            let model = row["model"] ?? nil,
            let color = row["color"] ?? nil,
            let yearStr = row["year"] ?? nil, let year = Int(yearStr)
        else { return nil }
        return Car(
            id: id, make: make, model: model, color: color, year: year,
            created_at: row["created_at"] ?? nil,
            updated_at: row["updated_at"] ?? nil
        )
    }
}
