# /api/system data: uname(2) + statvfs(2) FFI plus /proc text parsing, shaped
# exactly like the python sibling's response (platform/psutil-free there too —
# it reads the same /proc files). Parsers are pure functions of file content
# so they test without a device.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path

from wendynet.jsonmini import json_escape

# glibc struct utsname: six NUL-padded 65-byte fields.
comptime _UTS_FIELD = 65
comptime _UTS_SYSNAME = 0
comptime _UTS_NODENAME = 1
comptime _UTS_MACHINE = 4

# struct statvfs on LP64: unsigned-long/fsblkcnt_t fields, 8 bytes each.
comptime _VFS_FRSIZE = 8
comptime _VFS_BLOCKS = 16
comptime _VFS_BFREE = 24
comptime _VFS_BAVAIL = 32
comptime _GIB = 1 << 30


def _cstr(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    out.append(0)
    return out^


def _zeroed(n: Int) -> List[UInt8]:
    var out = List[UInt8]()
    for _ in range(n):
        out.append(0)
    return out^


def _read_text(path: String) raises -> String:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var bytes = List[UInt8]()
    for b in data:
        bytes.append(b)
    return String(from_utf8=bytes)


def _uts_field(buf: List[UInt8], index: Int) raises -> String:
    var out = List[UInt8]()
    for i in range(_UTS_FIELD):
        var b = buf[index * _UTS_FIELD + i]
        if b == 0:
            break
        out.append(b)
    return String(from_utf8=out)


def _read_u64(buf: List[UInt8], offset: Int) -> Int:
    var v = 0
    for i in range(8):
        v |= Int(buf[offset + i]) << (8 * i)
    return v


def _meminfo_kb(text: String, key: String) raises -> Int:
    # "MemTotal:        7990272 kB" -> 7990272; -1 when absent.
    for line in text.split("\n"):
        var l = String(line)
        if l.startswith(key + ":"):
            var rest = String(String(l.split(":")[1]).strip())
            var num = String("")
            for cp in rest.codepoints():
                var c = Int(cp.to_u32())
                if c >= 48 and c <= 57:
                    num += String(cp)
                else:
                    break
            if num != "":
                return Int(num)
    return -1


def meminfo_json(text: String) raises -> String:
    var total_kb = _meminfo_kb(text, "MemTotal")
    var avail_kb = _meminfo_kb(text, "MemAvailable")
    var out = String("{")
    var first = True
    if total_kb >= 0:
        out += '"total":"' + String(total_kb // 1024) + ' MB"'
        first = False
    if avail_kb >= 0:
        if not first:
            out += ","
        out += '"free":"' + String(avail_kb // 1024) + ' MB"'
    if total_kb >= 0 and avail_kb >= 0:
        out += ',"used":"' + String(total_kb // 1024 - avail_kb // 1024) + ' MB"'
    return out + "}"


def format_uptime(text: String) raises -> String:
    # "93784.53 370496.28" -> "26h 3m"; "" when unparsable.
    var fields = String(text.strip()).split(" ")
    if len(fields) == 0 or String(fields[0]) == "":
        return String("")
    var secs: Float64
    try:
        secs = Float64(String(fields[0]))
    except:
        return String("")
    var total_minutes = Int(secs) // 60
    return String(total_minutes // 60) + "h " + String(total_minutes % 60) + "m"


def cpu_model_from_cpuinfo(text: String) raises -> String:
    for line in text.split("\n"):
        var l = String(line)
        if l.startswith("model name"):
            var parts = l.split(":")
            if len(parts) >= 2:
                return String(String(parts[1]).strip())
    return String("")


def cores_from_cpuinfo(text: String) raises -> Int:
    var n = 0
    for line in text.split("\n"):
        if String(line).startswith("processor"):
            n += 1
    return n


def _disk_json(root: String) raises -> String:
    var buf = _zeroed(128)
    var croot = _cstr(root)
    if external_call["statvfs", c_int](croot.unsafe_ptr(), buf.unsafe_ptr()) != 0:
        return String("{}")
    var frsize = _read_u64(buf, _VFS_FRSIZE)
    var blocks = _read_u64(buf, _VFS_BLOCKS)
    var bfree = _read_u64(buf, _VFS_BFREE)
    var bavail = _read_u64(buf, _VFS_BAVAIL)
    return (
        '{"total":"'
        + String(frsize * blocks // _GIB)
        + ' GB","used":"'
        + String(frsize * (blocks - bfree) // _GIB)
        + ' GB","free":"'
        + String(frsize * bavail // _GIB)
        + ' GB"}'
    )


def system_json() raises -> String:
    var uts = _zeroed(6 * _UTS_FIELD)
    var sysname = String("")
    var nodename = String("")
    var machine = String("")
    if external_call["uname", c_int](uts.unsafe_ptr()) == 0:
        sysname = _uts_field(uts, _UTS_SYSNAME)
        nodename = _uts_field(uts, _UTS_NODENAME)
        machine = _uts_field(uts, _UTS_MACHINE)

    var hostname = getenv("WENDY_HOSTNAME")
    if hostname == "":
        hostname = nodename

    var mem = String("{}")
    try:
        mem = meminfo_json(_read_text("/proc/meminfo"))
    except:
        pass

    var uptime = String("")
    try:
        uptime = format_uptime(_read_text("/proc/uptime"))
    except:
        pass

    var cpu_model = String("")
    var cores = 0
    try:
        var cpuinfo = _read_text("/proc/cpuinfo")
        cpu_model = cpu_model_from_cpuinfo(cpuinfo)
        cores = cores_from_cpuinfo(cpuinfo)
    except:
        pass
    if cpu_model == "":
        cpu_model = machine

    var disk = String("{}")
    try:
        disk = _disk_json("/")
    except:
        pass

    return (
        '{"hostname":"'
        + json_escape(hostname)
        + '","platform":"'
        + json_escape(sysname)
        + '","architecture":"'
        + json_escape(machine)
        + '","uptime":"'
        + json_escape(uptime)
        + '","memory":'
        + mem
        + ',"disk":'
        + disk
        + ',"cpu":{"model":"'
        + json_escape(cpu_model)
        + '","cores":'
        + String(cores)
        + "}}"
    )
