#if canImport(FoundationEssentials)
internal import FoundationEssentials
#else
internal import Foundation
#endif

#if canImport(SQLite3)
import SQLite3
#elseif canImport(CSQLite)
import CSQLite
#else
@_silgen_name("sqlite3_open")
func sqlite3_open(_ filename: UnsafePointer<CChar>?, _ ppDb: UnsafeMutablePointer<OpaquePointer?>?) -> Int32
@_silgen_name("sqlite3_close")
func sqlite3_close(_ db: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_exec")
func sqlite3_exec(_ db: OpaquePointer?, _ sql: UnsafePointer<CChar>?,
                  _ callback: (@convention(c) (UnsafeMutableRawPointer?, Int32,
                  UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?,
                  UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32)?,
                  _ context: UnsafeMutableRawPointer?,
                  _ errmsg: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32
@_silgen_name("sqlite3_prepare_v2")
func sqlite3_prepare_v2(_ db: OpaquePointer?, _ sql: UnsafePointer<CChar>?,
                        _ nByte: Int32, _ ppStmt: UnsafeMutablePointer<OpaquePointer?>?,
                        _ pzTail: UnsafeMutablePointer<UnsafePointer<CChar>?>?) -> Int32
@_silgen_name("sqlite3_step")
func sqlite3_step(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_finalize")
func sqlite3_finalize(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_column_count")
func sqlite3_column_count(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_column_name")
func sqlite3_column_name(_ stmt: OpaquePointer?, _ N: Int32) -> UnsafePointer<CChar>?
@_silgen_name("sqlite3_column_type")
func sqlite3_column_type(_ stmt: OpaquePointer?, _ N: Int32) -> Int32
@_silgen_name("sqlite3_column_int64")
func sqlite3_column_int64(_ stmt: OpaquePointer?, _ N: Int32) -> Int64
@_silgen_name("sqlite3_column_text")
func sqlite3_column_text(_ stmt: OpaquePointer?, _ N: Int32) -> UnsafePointer<UInt8>?
@_silgen_name("sqlite3_bind_text")
func sqlite3_bind_text(_ stmt: OpaquePointer?, _ idx: Int32,
                       _ value: UnsafePointer<CChar>?, _ n: Int32,
                       _ destructor: (@convention(c) (UnsafeMutableRawPointer?) -> Void)?) -> Int32
@_silgen_name("sqlite3_bind_int64")
func sqlite3_bind_int64(_ stmt: OpaquePointer?, _ idx: Int32, _ value: Int64) -> Int32
@_silgen_name("sqlite3_last_insert_rowid")
func sqlite3_last_insert_rowid(_ db: OpaquePointer?) -> Int64
@_silgen_name("sqlite3_changes")
func sqlite3_changes(_ db: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_errmsg")
func sqlite3_errmsg(_ db: OpaquePointer?) -> UnsafePointer<CChar>?

let SQLITE_OK: Int32       = 0
let SQLITE_ROW: Int32      = 100
let SQLITE_DONE: Int32     = 101
let SQLITE_INTEGER: Int32  = 1
let SQLITE_TEXT: Int32     = 3
let SQLITE_NULL: Int32     = 5
nonisolated(unsafe) let SQLITE_TRANSIENT = unsafeBitCast(-1, to: (@convention(c) (UnsafeMutableRawPointer?) -> Void).self)
#endif

final class SQLiteDB: @unchecked Sendable {
    private let db: OpaquePointer?

    init(path: String) throws {
        let dir = URL(filePath: path).deletingLastPathComponent().path()
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        var handle: OpaquePointer?
        guard sqlite3_open(path, &handle) == SQLITE_OK else {
            let msg = handle.flatMap { sqlite3_errmsg($0) }.map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.open(msg)
        }
        self.db = handle
    }

    deinit { _ = sqlite3_close(db) }

    @discardableResult
    func exec(_ sql: String) throws -> [[String: String?]] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.prepare(msg)
        }
        defer { _ = sqlite3_finalize(stmt) }

        var rows: [[String: String?]] = []
        let colCount = sqlite3_column_count(stmt)
        while sqlite3_step(stmt) == SQLITE_ROW {
            var row: [String: String?] = [:]
            for i in 0..<colCount {
                let name = sqlite3_column_name(stmt, i).map { String(cString: $0) } ?? "column_\(i)"
                let type = sqlite3_column_type(stmt, i)
                if type == SQLITE_NULL {
                    row[name] = nil
                } else if type == SQLITE_INTEGER {
                    row[name] = String(sqlite3_column_int64(stmt, i))
                } else {
                    row[name] = sqlite3_column_text(stmt, i).map { String(cString: $0) }
                }
            }
            rows.append(row)
        }
        return rows
    }

    @discardableResult
    func run(_ sql: String, bindings: [Any?] = []) throws -> Int64 {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.prepare(msg)
        }
        defer { _ = sqlite3_finalize(stmt) }

        for (i, value) in bindings.enumerated() {
            let idx = Int32(i + 1)
            switch value {
            case let v as String:
                _ = sqlite3_bind_text(stmt, idx, v, -1, SQLITE_TRANSIENT)
            case let v as Int:
                _ = sqlite3_bind_int64(stmt, idx, Int64(v))
            case let v as Int64:
                _ = sqlite3_bind_int64(stmt, idx, v)
            default:
                break
            }
        }

        let rc = sqlite3_step(stmt)
        guard rc == SQLITE_DONE || rc == SQLITE_ROW else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.step(msg)
        }
        return sqlite3_last_insert_rowid(db)
    }

    var changes: Int32 { sqlite3_changes(db) }
}

enum SQLiteError: Error, CustomStringConvertible {
    case open(String), prepare(String), step(String)
    var description: String {
        switch self {
        case .open(let m): "sqlite3_open: \(m)"
        case .prepare(let m): "sqlite3_prepare: \(m)"
        case .step(let m): "sqlite3_step: \(m)"
        }
    }
}
