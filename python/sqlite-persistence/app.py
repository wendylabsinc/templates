"""
SQLite Persistence Example

Demonstrates using SQLite with a persistent volume that survives container restarts.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("/data")
DB_PATH = DATA_DIR / "app.db"


def main():
    print("SQLite Persistence Example")
    print("=" * 40)

    # Ensure the data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Connect to SQLite database (creates if not exists)
    print(f"\nConnecting to database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)

    # Insert a new record for this run
    timestamp = datetime.now().isoformat()
    message = f"Application started at {timestamp}"
    cursor.execute(
        "INSERT INTO runs (timestamp, message) VALUES (?, ?)",
        (timestamp, message)
    )
    conn.commit()
    print(f"Inserted: {message}")

    # Query all records
    print("\n" + "-" * 40)
    print("All recorded runs:")
    print("-" * 40)
    cursor.execute("SELECT id, timestamp, message FROM runs ORDER BY id")
    rows = cursor.fetchall()
    for row in rows:
        print(f"  [{row[0]}] {row[1]}: {row[2]}")

    print(f"\nTotal runs: {len(rows)}")
    print("-" * 40)

    # Close connection
    conn.close()

    print("\nSQLite persistence example complete!")
    print("Run this again to see the data persist across restarts.")


if __name__ == "__main__":
    main()
