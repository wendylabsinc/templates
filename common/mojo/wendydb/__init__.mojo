# wendydb: SQLite for the Mojo templates via libsqlite3.so.0 dlopen — no
# link-time dependency, no C struct ABI. Mojo 1.0 has no stdlib database
# layer, so persistence goes through the C library directly.
from .sqlite import Db, Stmt
