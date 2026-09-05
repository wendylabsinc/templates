# wendydb tests: real libsqlite3 through the OwnedDLHandle wrapper — schema
# creation, bound inserts, typed column reads, NULL detection, change counts,
# error surfacing, and on-disk persistence across close/reopen. Runs against
# the distro libsqlite3.so.0 inside the test container; no hardware needed.
from std.ffi import external_call, c_int

from wendydb.sqlite import Db


comptime SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    make TEXT NOT NULL,
    year INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
)
"""


def main() raises:
    var path = String("/tmp/test_wendydb.db")
    # Fresh run: remove any leftover file so AUTOINCREMENT ids start at 1.
    # unlinkat instead of unlink: the stdlib may already declare common libc
    # externs, and duplicate declarations fail LLVM lowering (MMF-020).
    var cpath = List[UInt8]()
    for b in path.as_bytes():
        cpath.append(b)
    cpath.append(0)
    _ = external_call["unlinkat", c_int](c_int(-100), cpath.unsafe_ptr(), c_int(0))

    var db = Db.open(path)
    db.exec(SCHEMA)

    # --- bound insert + last_insert_rowid ---
    var ins = db.prepare("INSERT INTO cars (make, year) VALUES (?, ?)")
    ins.bind_text(1, "O'Brien & Söhne")  # quote + non-ASCII survive binding
    ins.bind_int(2, 2024)
    if ins.step():
        raise Error("INSERT step should report done, not a row")
    ins.finalize()
    var rowid = db.last_insert_rowid()
    if rowid != 1:
        raise Error("first rowid should be 1, got " + String(rowid))
    print("PASS: bound insert -> rowid 1")

    # --- typed reads + NULL detection ---
    var q = db.prepare(
        "SELECT id, make, year, created_at, updated_at FROM cars WHERE id = ?"
    )
    q.bind_int(1, rowid)
    if not q.step():
        raise Error("SELECT should return the inserted row")
    if q.column_int(0) != 1:
        raise Error("id column wrong")
    if q.column_text(1) != "O'Brien & Söhne":
        raise Error("make column wrong: " + q.column_text(1))
    if q.column_int(2) != 2024:
        raise Error("year column wrong")
    if q.column_is_null(3) or q.column_text(3) == "":
        raise Error("created_at default should be populated")
    if not q.column_is_null(4):
        raise Error("updated_at should be NULL")
    if q.step():
        raise Error("only one row expected")
    q.finalize()
    print("PASS: typed column reads + NULL detection")

    # --- update reports changes ---
    var upd = db.prepare("UPDATE cars SET year = ? WHERE id = ?")
    upd.bind_int(1, 1999)
    upd.bind_int(2, rowid)
    _ = upd.step()
    upd.finalize()
    if db.changes() != 1:
        raise Error("update should change 1 row, got " + String(db.changes()))

    var miss = db.prepare("DELETE FROM cars WHERE id = ?")
    miss.bind_int(1, 4242)
    _ = miss.step()
    miss.finalize()
    if db.changes() != 0:
        raise Error("delete of missing id should change 0 rows")
    print("PASS: changes() counts (1 update, 0 missing-delete)")

    # --- errors surface as raises, not silent garbage ---
    var raised = False
    try:
        db.exec("INSERT INTO no_such_table VALUES (1)")
    except:
        raised = True
    if not raised:
        raise Error("bad SQL should raise")
    raised = False
    try:
        var bad = db.prepare("SELECT nope FROM cars")
        bad.finalize()
    except:
        raised = True
    if not raised:
        raise Error("prepare of bad column should raise")
    print("PASS: SQL errors raise")

    # --- persistence across close/reopen (the /data volume story) ---
    db.close()
    var db2 = Db.open(path)
    var q2 = db2.prepare("SELECT year FROM cars WHERE id = 1")
    if not q2.step():
        raise Error("row should survive close/reopen")
    if q2.column_int(0) != 1999:
        raise Error("updated value should persist, got " + String(q2.column_int(0)))
    q2.finalize()
    db2.close()
    print("PASS: on-disk persistence across reopen")
