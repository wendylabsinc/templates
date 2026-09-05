# SQLite through libsqlite3.so.0 via OwnedDLHandle. The C API is pointer-in,
# pointer-out with no struct ABI, so the wrapper stays thin: Db owns a
# sqlite3*, Stmt owns a sqlite3_stmt*, both as opaque Ints. Statements bind
# with SQLITE_TRANSIENT so temporary Mojo buffers are safe to pass.
from std.ffi import OwnedDLHandle, external_call, c_int

comptime _SQLITE_ROW = 100
comptime _SQLITE_DONE = 101
comptime _SQLITE_NULL = 5
comptime _SQLITE_TRANSIENT = -1  # destructor sentinel: sqlite copies the bytes
comptime _ERRMSG_CAP = 512


def _cstr(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    out.append(0)
    return out^


def _read_cstring(addr: Int) -> String:
    # Byte-at-a-time copy up to NUL: no strlen extern (the stdlib may declare
    # it; duplicate extern declarations fail LLVM lowering — MMF-020) and no
    # over-read past the allocation.
    var out = List[UInt8]()
    var byte = List[UInt8]()
    byte.append(0)
    for i in range(_ERRMSG_CAP):
        _ = external_call["memcpy", Int](byte.unsafe_ptr(), addr + i, 1)
        if byte[0] == 0:
            break
        out.append(byte[0])
    var s: String
    try:
        s = String(from_utf8=out)
    except:
        s = String("<invalid utf-8>")
    return s


struct Stmt(Movable):
    var lib: OwnedDLHandle  # own dlopen ref; the loader refcounts the .so
    var stmt: Int
    var db: Int  # borrowed sqlite3* for error messages only

    def __init__(out self, var lib: OwnedDLHandle, stmt: Int, db: Int):
        self.lib = lib^
        self.stmt = stmt
        self.db = db

    def _errmsg(mut self) -> String:
        var ptr = Int(self.lib.call["sqlite3_errmsg", Int](self.db))
        if ptr == 0:
            return String("")
        return _read_cstring(ptr)

    def bind_text(mut self, index: Int, value: String) raises:
        var bytes = _cstr(value)  # trailing NUL keeps the ptr non-null for ""
        var rc = Int(
            self.lib.call["sqlite3_bind_text", c_int](
                self.stmt,
                c_int(index),
                bytes.unsafe_ptr(),
                c_int(len(bytes) - 1),
                Int(_SQLITE_TRANSIENT),
            )
        )
        if rc != 0:
            raise Error("sqlite3_bind_text failed: " + self._errmsg())

    def bind_int(mut self, index: Int, value: Int) raises:
        var rc = Int(
            self.lib.call["sqlite3_bind_int64", c_int](
                self.stmt, c_int(index), value
            )
        )
        if rc != 0:
            raise Error("sqlite3_bind_int64 failed: " + self._errmsg())

    def step(mut self) raises -> Bool:
        # True: a result row is available. False: statement finished.
        var rc = Int(self.lib.call["sqlite3_step", c_int](self.stmt))
        if rc == _SQLITE_ROW:
            return True
        if rc == _SQLITE_DONE:
            return False
        raise Error("sqlite3_step failed: " + self._errmsg())

    def column_int(mut self, index: Int) -> Int:
        return Int(
            self.lib.call["sqlite3_column_int64", Int](self.stmt, c_int(index))
        )

    def column_text(mut self, index: Int) raises -> String:
        # column_text before column_bytes, per the C API contract.
        var ptr = Int(
            self.lib.call["sqlite3_column_text", Int](self.stmt, c_int(index))
        )
        var n = Int(
            self.lib.call["sqlite3_column_bytes", c_int](self.stmt, c_int(index))
        )
        if ptr == 0 or n <= 0:
            return String("")
        var buf = List[UInt8](unsafe_uninit_length=n)
        _ = external_call["memcpy", Int](buf.unsafe_ptr(), ptr, n)
        return String(from_utf8=buf)

    def column_is_null(mut self, index: Int) -> Bool:
        var t = Int(
            self.lib.call["sqlite3_column_type", c_int](self.stmt, c_int(index))
        )
        return t == _SQLITE_NULL

    def finalize(mut self):
        if self.stmt != 0:
            _ = self.lib.call["sqlite3_finalize", c_int](self.stmt)
            self.stmt = 0


struct Db(Movable):
    var lib: OwnedDLHandle
    var db: Int

    def __init__(out self, var lib: OwnedDLHandle, db: Int):
        self.lib = lib^
        self.db = db

    @staticmethod
    def open(path: String) raises -> Db:
        var lib = OwnedDLHandle("libsqlite3.so.0")
        var handle_out = List[Int]()
        handle_out.append(0)
        var cpath = _cstr(path)
        var rc = Int(
            lib.call["sqlite3_open", c_int](
                cpath.unsafe_ptr(), handle_out.unsafe_ptr()
            )
        )
        if rc != 0:
            # Per the C API, a handle may come back even on failure and must
            # still be closed.
            if handle_out[0] != 0:
                _ = lib.call["sqlite3_close", c_int](handle_out[0])
            raise Error("sqlite3_open(" + path + ") failed: rc=" + String(rc))
        return Db(lib^, handle_out[0])

    def _errmsg(mut self) -> String:
        var ptr = Int(self.lib.call["sqlite3_errmsg", Int](self.db))
        if ptr == 0:
            return String("")
        return _read_cstring(ptr)

    def exec(mut self, sql: String) raises:
        var csql = _cstr(sql)
        var rc = Int(
            self.lib.call["sqlite3_exec", c_int](
                self.db, csql.unsafe_ptr(), Int(0), Int(0), Int(0)
            )
        )
        if rc != 0:
            raise Error("sqlite3_exec failed: " + self._errmsg())

    def prepare(mut self, sql: String) raises -> Stmt:
        var stmt_out = List[Int]()
        stmt_out.append(0)
        var csql = _cstr(sql)
        var rc = Int(
            self.lib.call["sqlite3_prepare_v2", c_int](
                self.db,
                csql.unsafe_ptr(),
                c_int(-1),
                stmt_out.unsafe_ptr(),
                Int(0),
            )
        )
        if rc != 0:
            raise Error("sqlite3_prepare_v2 failed: " + self._errmsg())
        return Stmt(OwnedDLHandle("libsqlite3.so.0"), stmt_out[0], self.db)

    def last_insert_rowid(mut self) -> Int:
        return Int(self.lib.call["sqlite3_last_insert_rowid", Int](self.db))

    def changes(mut self) -> Int:
        return Int(self.lib.call["sqlite3_changes", c_int](self.db))

    def close(mut self):
        if self.db != 0:
            _ = self.lib.call["sqlite3_close", c_int](self.db)
            self.db = 0
