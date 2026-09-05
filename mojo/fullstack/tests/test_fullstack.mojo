# fullstack backend tests: cars CRUD JSON against a real SQLite file (same
# shapes the React persistence page consumes) and the pure /api/system
# parsers on fixture text. Camera/audio/WS paths are covered by the shared
# package tests and on-device verification.
from std.ffi import external_call, c_int
from std.os import getenv

from carstore import (
    car_create,
    car_delete,
    car_get_json,
    car_update,
    cars_list_json,
)
from gpudiag import thermal_fallback_json
from sysinfo import (
    cores_from_cpuinfo,
    cpu_model_from_cpuinfo,
    format_uptime,
    meminfo_json,
    system_json,
)


def unlink(path: String):
    var cpath = List[UInt8]()
    for b in path.as_bytes():
        cpath.append(b)
    cpath.append(0)
    _ = external_call["unlinkat", c_int](c_int(-100), cpath.unsafe_ptr(), c_int(0))


def expect_contains(hay: String, needle: String, what: String) raises:
    if hay.find(needle) < 0:
        raise Error(what + ": missing " + needle + " in " + hay)


def main() raises:
    var db = String("/tmp/test_fullstack_cars.db")
    unlink(db)

    # --- create returns the full row ---
    var row = car_create(db, 'Say "hi"', "Model 3", "#3b82f6", 2024)
    expect_contains(row, '"id":1', "create")
    expect_contains(row, '"make":"Say \\"hi\\""', "create escapes quotes")
    expect_contains(row, '"model":"Model 3"', "create")
    expect_contains(row, '"color":"#3b82f6"', "create")
    expect_contains(row, '"year":2024', "create")
    expect_contains(row, '"created_at":"', "create sets created_at")
    expect_contains(row, '"updated_at":null', "create leaves updated_at null")
    print("PASS: create -> full row json")

    # --- list wraps rows in an array ---
    _ = car_create(db, "Honda", "Civic", "red", 1998)
    var listing = cars_list_json(db)
    if not listing.startswith("[") or not listing.endswith("]"):
        raise Error("list should be a JSON array: " + listing)
    expect_contains(listing, '"id":1', "list")
    expect_contains(listing, '"id":2', "list")
    print("PASS: list -> array of rows")

    # --- get: found and missing ---
    expect_contains(car_get_json(db, 2), '"make":"Honda"', "get")
    if car_get_json(db, 999) != "":
        raise Error("get of missing id should be empty")
    print("PASS: get -> row / empty for missing")

    # --- update rewrites fields and stamps updated_at ---
    var updated = car_update(db, 2, "Honda", "Accord", "blue", 2001)
    expect_contains(updated, '"model":"Accord"', "update")
    expect_contains(updated, '"updated_at":"', "update stamps updated_at")
    if car_update(db, 999, "X", "Y", "Z", 1) != "":
        raise Error("update of missing id should be empty")
    print("PASS: update -> row / empty for missing")

    # --- delete: True once, False after ---
    if not car_delete(db, 1):
        raise Error("delete of existing id should be True")
    if car_delete(db, 1):
        raise Error("second delete should be False")
    expect_contains(cars_list_json(db), '"id":2', "survivor remains")
    print("PASS: delete -> True/False")

    # --- /api/system pure parsers ---
    var mem = meminfo_json(
        "MemTotal:        7990272 kB\nMemFree:          500000 kB\n"
        + "MemAvailable:    4200000 kB\nBuffers:          100000 kB\n"
    )
    expect_contains(mem, '"total":"7803 MB"', "meminfo total")
    expect_contains(mem, '"free":"4101 MB"', "meminfo free")
    expect_contains(mem, '"used":"3702 MB"', "meminfo used")
    if meminfo_json("") != "{}":
        raise Error("empty meminfo should be {}")
    print("PASS: meminfo parsing")

    if format_uptime("93784.53 370496.28\n") != "26h 3m":
        raise Error("uptime wrong: " + format_uptime("93784.53 370496.28\n"))
    if format_uptime("") != "":
        raise Error("empty uptime should be empty")
    print("PASS: uptime formatting")

    var cpuinfo = (
        "processor\t: 0\nmodel name\t: ARMv8 Processor rev 1 (v8l)\n\n"
        + "processor\t: 1\nmodel name\t: ARMv8 Processor rev 1 (v8l)\n"
    )
    if cpu_model_from_cpuinfo(cpuinfo) != "ARMv8 Processor rev 1 (v8l)":
        raise Error("cpu model wrong: " + cpu_model_from_cpuinfo(cpuinfo))
    if cores_from_cpuinfo(cpuinfo) != 2:
        raise Error("core count wrong")
    print("PASS: cpuinfo parsing")

    # --- assembled system_json inside the linux container ---
    var sys = system_json()
    expect_contains(sys, '"platform":"Linux"', "system_json")
    expect_contains(sys, '"architecture":"', "system_json")
    expect_contains(sys, '"hostname":"', "system_json")
    expect_contains(sys, '"memory":{', "system_json")
    expect_contains(sys, '"disk":{', "system_json")
    expect_contains(sys, '"cpu":{', "system_json")
    expect_contains(sys, '"uptime":"', "system_json")
    print("PASS: system_json assembles on linux")

    # --- /api/gpu thermal fallback (the non-CUDA path) ---
    var fixtures = getenv("FIXTURES")
    if fixtures == "":
        raise Error("set FIXTURES=/path/to/tests/fixtures")
    var gpu = thermal_fallback_json(fixtures + "/thermal")
    expect_contains(gpu, '"available":true', "thermal fallback")
    expect_contains(gpu, '"name":"ARM GPU"', "thermal fallback")
    # zone1 is the gpu-typed zone (46200 -> 46.2°C); zone0 must not win.
    expect_contains(gpu, '"temperature":"46.2°C"', "thermal picks gpu zone")
    if thermal_fallback_json(fixtures + "/does-not-exist") != '{"available":false}':
        raise Error("missing thermal root should report unavailable")
    print("PASS: gpu thermal fallback")
