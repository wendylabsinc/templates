/**
 * SQLite Persistence Example
 *
 * Demonstrates using SQLite with a persistent volume that survives container restarts.
 */

import Database from "better-sqlite3";
import * as fs from "fs";
import * as path from "path";

const DATA_DIR = "/data";
const DB_PATH = path.join(DATA_DIR, "app.db");

interface Run {
  id: number;
  timestamp: string;
  message: string;
}

function main(): void {
  console.log("SQLite Persistence Example");
  console.log("=".repeat(40));

  // Ensure the data directory exists
  fs.mkdirSync(DATA_DIR, { recursive: true });

  // Connect to SQLite database (creates if not exists)
  console.log(`\nConnecting to database: ${DB_PATH}`);
  const db = new Database(DB_PATH);

  // Create table if it doesn't exist
  db.exec(`
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL,
      message TEXT NOT NULL
    )
  `);

  // Insert a new record for this run
  const timestamp = new Date().toISOString();
  const message = `Application started at ${timestamp}`;
  const insert = db.prepare(
    "INSERT INTO runs (timestamp, message) VALUES (?, ?)"
  );
  insert.run(timestamp, message);
  console.log(`Inserted: ${message}`);

  // Query all records
  console.log("\n" + "-".repeat(40));
  console.log("All recorded runs:");
  console.log("-".repeat(40));

  const select = db.prepare("SELECT id, timestamp, message FROM runs ORDER BY id");
  const rows = select.all() as Run[];

  for (const row of rows) {
    console.log(`  [${row.id}] ${row.timestamp}: ${row.message}`);
  }

  console.log(`\nTotal runs: ${rows.length}`);
  console.log("-".repeat(40));

  // Close connection
  db.close();

  console.log("\nSQLite persistence example complete!");
  console.log("Run this again to see the data persist across restarts.");
}

main();
