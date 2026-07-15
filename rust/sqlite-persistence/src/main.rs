//! SQLite Persistence Example
//!
//! Demonstrates using SQLite with a persistent volume that survives container restarts.

use chrono::Utc;
use rusqlite::{Connection, Result};
use std::fs;
use std::path::Path;

const DATA_DIR: &str = "/data";

#[derive(Debug)]
struct Run {
    id: i32,
    timestamp: String,
    message: String,
}

fn main() -> Result<()> {
    println!("SQLite Persistence Example");
    println!("{}", "=".repeat(40));

    let data_path = Path::new(DATA_DIR);
    let db_path = data_path.join("app.db");

    // Ensure the data directory exists
    fs::create_dir_all(data_path).expect("Failed to create data directory");

    // Connect to SQLite database (creates if not exists)
    println!("\nConnecting to database: {}", db_path.display());
    let conn = Connection::open(&db_path)?;

    // Create table if it doesn't exist
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL
        )",
        [],
    )?;

    // Insert a new record for this run
    let timestamp = Utc::now().to_rfc3339();
    let message = format!("Application started at {}", timestamp);
    conn.execute(
        "INSERT INTO runs (timestamp, message) VALUES (?1, ?2)",
        [&timestamp, &message],
    )?;
    println!("Inserted: {}", message);

    // Query all records
    println!("\n{}", "-".repeat(40));
    println!("All recorded runs:");
    println!("{}", "-".repeat(40));

    let mut stmt = conn.prepare("SELECT id, timestamp, message FROM runs ORDER BY id")?;
    let runs = stmt.query_map([], |row| {
        Ok(Run {
            id: row.get(0)?,
            timestamp: row.get(1)?,
            message: row.get(2)?,
        })
    })?;

    let mut count = 0;
    for run in runs {
        let run = run?;
        println!("  [{}] {}: {}", run.id, run.timestamp, run.message);
        count += 1;
    }

    println!("\nTotal runs: {}", count);
    println!("{}", "-".repeat(40));

    println!("\nSQLite persistence example complete!");
    println!("Run this again to see the data persist across restarts.");

    Ok(())
}
