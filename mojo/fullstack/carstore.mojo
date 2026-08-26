# Cars CRUD on SQLite (wendydb), producing the exact JSON row shapes the
# python/fullstack FastAPI backend returns. Connections are per-operation,
# like the sibling's get_db(): open, ensure schema, act, close — SQLite makes
# that cheap and it keeps the single-threaded server stateless between
# requests.
from wendydb.sqlite import Db, Stmt
from wendynet.jsonmini import json_escape

comptime _SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    color TEXT NOT NULL,
    year INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
)
"""
comptime _ROW_COLS = "id, make, model, color, year, created_at, updated_at"


def _open(path: String) raises -> Db:
    var db = Db.open(path)
    db.exec(_SCHEMA)
    return db^


def _row_json(mut st: Stmt) raises -> String:
    var updated = String("null")
    if not st.column_is_null(6):
        updated = '"' + json_escape(st.column_text(6)) + '"'
    return (
        '{"id":'
        + String(st.column_int(0))
        + ',"make":"'
        + json_escape(st.column_text(1))
        + '","model":"'
        + json_escape(st.column_text(2))
        + '","color":"'
        + json_escape(st.column_text(3))
        + '","year":'
        + String(st.column_int(4))
        + ',"created_at":"'
        + json_escape(st.column_text(5))
        + '","updated_at":'
        + updated
        + "}"
    )


def _get_row(mut db: Db, id: Int) raises -> String:
    # Row JSON, or "" when the id does not exist.
    var st = db.prepare("SELECT " + _ROW_COLS + " FROM cars WHERE id = ?")
    st.bind_int(1, id)
    var out = String("")
    if st.step():
        out = _row_json(st)
    st.finalize()
    return out


def cars_list_json(path: String) raises -> String:
    var db = _open(path)
    var st = db.prepare("SELECT " + _ROW_COLS + " FROM cars ORDER BY id")
    var out = String("[")
    var first = True
    while st.step():
        if not first:
            out += ","
        first = False
        out += _row_json(st)
    st.finalize()
    db.close()
    return out + "]"


def car_get_json(path: String, id: Int) raises -> String:
    var db = _open(path)
    var out = _get_row(db, id)
    db.close()
    return out


def car_create(
    path: String, make: String, model: String, color: String, year: Int
) raises -> String:
    var db = _open(path)
    var st = db.prepare(
        "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)"
    )
    st.bind_text(1, make)
    st.bind_text(2, model)
    st.bind_text(3, color)
    st.bind_int(4, year)
    _ = st.step()
    st.finalize()
    var out = _get_row(db, db.last_insert_rowid())
    db.close()
    return out


def car_update(
    path: String, id: Int, make: String, model: String, color: String, year: Int
) raises -> String:
    # Updated row JSON, or "" when the id does not exist.
    var db = _open(path)
    var st = db.prepare(
        "UPDATE cars SET make=?, model=?, color=?, year=?,"
        " updated_at=datetime('now') WHERE id=?"
    )
    st.bind_text(1, make)
    st.bind_text(2, model)
    st.bind_text(3, color)
    st.bind_int(4, year)
    st.bind_int(5, id)
    _ = st.step()
    st.finalize()
    var out = _get_row(db, id)
    db.close()
    return out


def car_delete(path: String, id: Int) raises -> Bool:
    var db = _open(path)
    var st = db.prepare("DELETE FROM cars WHERE id = ?")
    st.bind_int(1, id)
    _ = st.step()
    st.finalize()
    var deleted = db.changes() == 1
    db.close()
    return deleted
