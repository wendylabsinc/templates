/**
 * SQLite Persistence Example
 *
 * Demonstrates using SQLite with a persistent volume that survives container restarts.
 */

#include <iostream>
#include <filesystem>
#include <string>
#include <ctime>
#include <iomanip>
#include <sstream>
#include <sqlite3.h>

namespace fs = std::filesystem;

const std::string DATA_DIR = "/data";

std::string get_timestamp() {
    auto now = std::time(nullptr);
    auto tm = *std::gmtime(&now);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%dT%H:%M:%SZ");
    return oss.str();
}

int main() {
    std::cout << "SQLite Persistence Example" << std::endl;
    std::cout << std::string(40, '=') << std::endl;

    fs::path data_path(DATA_DIR);
    fs::path db_path = data_path / "app.db";

    // Ensure the data directory exists
    fs::create_directories(data_path);

    // Connect to SQLite database
    std::cout << "\nConnecting to database: " << db_path << std::endl;

    sqlite3* db;
    int rc = sqlite3_open(db_path.c_str(), &db);
    if (rc != SQLITE_OK) {
        std::cerr << "Cannot open database: " << sqlite3_errmsg(db) << std::endl;
        return 1;
    }

    // Create table if it doesn't exist
    const char* create_sql = R"(
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL
        )
    )";

    char* err_msg = nullptr;
    rc = sqlite3_exec(db, create_sql, nullptr, nullptr, &err_msg);
    if (rc != SQLITE_OK) {
        std::cerr << "SQL error: " << err_msg << std::endl;
        sqlite3_free(err_msg);
        sqlite3_close(db);
        return 1;
    }

    // Insert a new record for this run
    std::string timestamp = get_timestamp();
    std::string message = "Application started at " + timestamp;

    sqlite3_stmt* stmt;
    const char* insert_sql = "INSERT INTO runs (timestamp, message) VALUES (?, ?)";
    rc = sqlite3_prepare_v2(db, insert_sql, -1, &stmt, nullptr);
    if (rc == SQLITE_OK) {
        sqlite3_bind_text(stmt, 1, timestamp.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 2, message.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_step(stmt);
        sqlite3_finalize(stmt);
        std::cout << "Inserted: " << message << std::endl;
    }

    // Query all records
    std::cout << "\n" << std::string(40, '-') << std::endl;
    std::cout << "All recorded runs:" << std::endl;
    std::cout << std::string(40, '-') << std::endl;

    const char* select_sql = "SELECT id, timestamp, message FROM runs ORDER BY id";
    rc = sqlite3_prepare_v2(db, select_sql, -1, &stmt, nullptr);

    int count = 0;
    if (rc == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            int id = sqlite3_column_int(stmt, 0);
            const char* ts = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1));
            const char* msg = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 2));
            std::cout << "  [" << id << "] " << ts << ": " << msg << std::endl;
            count++;
        }
        sqlite3_finalize(stmt);
    }

    std::cout << "\nTotal runs: " << count << std::endl;
    std::cout << std::string(40, '-') << std::endl;

    // Close connection
    sqlite3_close(db);

    std::cout << "\nSQLite persistence example complete!" << std::endl;
    std::cout << "Run this again to see the data persist across restarts." << std::endl;

    return 0;
}
