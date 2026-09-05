# /api/gpu: instead of shelling out to nvidia-smi like the python sibling,
# a GPU build proves the GPU works — it runs a Mojo matmul kernel through
# MAX's DeviceContext (gpu-hello's approach) and reports the measured GFLOPS
# alongside the fields the frontend renders. CPU builds (and any runtime GPU
# failure) fall back to the sibling's thermal-zone path. The kernel result is
# cached by the caller: CUDA context creation is a one-time cost.
from std.math import ceildiv
from std.pathlib import Path
from std.sys import has_accelerator
from std.time import perf_counter_ns

from max.gpu.host import DeviceContext
from std.gpu import block_dim, block_idx, thread_idx
from layout import TileTensor, row_major

from wendynet.jsonmini import json_escape

comptime float_dtype = DType.float32
comptime mat_n = 512
comptime mat_layout = row_major[mat_n, mat_n]()
comptime _MAX_THERMAL_ZONES = 20


def matmul_kernel(
    a: TileTensor[float_dtype, type_of(mat_layout), MutAnyOrigin],
    b: TileTensor[float_dtype, type_of(mat_layout), MutAnyOrigin],
    c: TileTensor[float_dtype, type_of(mat_layout), MutAnyOrigin],
):
    var row = block_idx.y * block_dim.y + thread_idx.y
    var col = block_idx.x * block_dim.x + thread_idx.x
    if row < mat_n and col < mat_n:
        var acc: Float32 = 0
        for k in range(mat_n):
            acc += a[row, k] * b[k, col]
        c[row, col] = acc


def _read_first_line(path: String) raises -> String:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var out = List[UInt8]()
    for b in data:
        if b == 10:
            break
        out.append(b)
    return String(from_utf8=out)


def _thermal_temp(root: String) -> String:
    # Millidegrees from the first gpu-typed zone (zone0 as fallback) -> "46.2°C".
    var zone = String("")
    var fallback = String("")
    for i in range(_MAX_THERMAL_ZONES):
        var base = root + "/thermal_zone" + String(i)
        try:
            var t = _read_first_line(base + "/type").lower()
            if i == 0:
                fallback = base
            if t.find("gpu") >= 0:
                zone = base
                break
        except:
            continue
    if zone == "":
        zone = fallback
    if zone == "":
        return String("")
    try:
        var milli = Int(String(_read_first_line(zone + "/temp").strip()))
        return String(milli // 1000) + "." + String((milli % 1000) // 100) + "°C"
    except:
        return String("")


def thermal_fallback_json(root: String) raises -> String:
    # Same shape the python sibling reports when nvidia-smi is absent.
    var temp = _thermal_temp(root)
    if temp == "":
        return String('{"available":false}')
    return '{"available":true,"name":"ARM GPU","temperature":"' + temp + '"}'


def gpu_probe_json(thermal_root: String) raises -> String:
    comptime if not has_accelerator():
        return thermal_fallback_json(thermal_root)
    else:
        try:
            var ctx = DeviceContext()
            var a_host = ctx.enqueue_create_host_buffer[float_dtype](mat_n * mat_n)
            var b_host = ctx.enqueue_create_host_buffer[float_dtype](mat_n * mat_n)
            ctx.synchronize()
            for i in range(mat_n * mat_n):
                a_host[i] = 1.0
                b_host[i] = 1.0
            var a_dev = ctx.enqueue_create_buffer[float_dtype](mat_n * mat_n)
            var b_dev = ctx.enqueue_create_buffer[float_dtype](mat_n * mat_n)
            var c_dev = ctx.enqueue_create_buffer[float_dtype](mat_n * mat_n)
            ctx.enqueue_copy(dst_buf=a_dev, src_buf=a_host)
            ctx.enqueue_copy(dst_buf=b_dev, src_buf=b_host)
            var a_t = TileTensor(a_dev, mat_layout)
            var b_t = TileTensor(b_dev, mat_layout)
            var c_t = TileTensor(c_dev, mat_layout)

            comptime tile = 16
            comptime blocks = ceildiv(mat_n, tile)
            # warm launch (includes JIT/module load), then timed
            ctx.enqueue_function[matmul_kernel](
                a_t, b_t, c_t, grid_dim=(blocks, blocks), block_dim=(tile, tile)
            )
            ctx.synchronize()
            var t0 = perf_counter_ns()
            ctx.enqueue_function[matmul_kernel](
                a_t, b_t, c_t, grid_dim=(blocks, blocks), block_dim=(tile, tile)
            )
            ctx.synchronize()
            var mm_s = Float64(perf_counter_ns() - t0) / 1e9
            var gflops = (
                2.0 * Float64(mat_n) * Float64(mat_n) * Float64(mat_n) / mm_s / 1e9
            )

            var c_host = ctx.enqueue_create_host_buffer[float_dtype](mat_n * mat_n)
            ctx.enqueue_copy(dst_buf=c_host, src_buf=c_dev)
            ctx.synchronize()
            var errors = 0
            for i in range(mat_n * mat_n):
                if c_host[i] != Float32(mat_n):
                    errors += 1
            if errors != 0:
                raise Error("matmul verify failed: " + String(errors) + " errors")

            var out = String('{"available":true,"name":"')
            out += json_escape(ctx.name())
            out += '","driver":"' + json_escape(ctx.api()) + " / Mojo DeviceContext"
            var temp = _thermal_temp(thermal_root)
            if temp != "":
                out += '","temperature":"' + temp
            out += '","kernel":"matmul ' + String(mat_n) + "x" + String(mat_n)
            out += " verified, " + String(Int(gflops)) + ' GFLOPS"}'
            return out
        except e:
            # Built for GPU but the runtime probe failed: report the sibling's
            # fallback shape plus the reason for debuggability.
            var fb = thermal_fallback_json(thermal_root)
            return (
                String(fb[byte = 0 : fb.byte_length() - 1])
                + ',"error":"'
                + json_escape(String(e))
                + '"}'
            )
