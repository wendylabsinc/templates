import sqlite3
from pathlib import Path

DB_PATH = Path("/data/cars.db")


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            color TEXT NOT NULL,
            year INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn
