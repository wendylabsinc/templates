# gpu-hello: prove Modular's stack drives this device's GPU, then serve the
# evidence over HTTP. Pure Mojo — the HTTP layer is libc-FFI sockets because
# Mojo 1.0 has no stdlib networking (see docs/mojo-max-port-findings.md).
from std.ffi import external_call, c_int, c_ssize_t
from std.math import ceildiv
from std.sys import has_accelerator
from std.time import perf_counter_ns

comptime float_dtype = DType.float32
comptime vec_n = 1 << 20
comptime mat_n = 512
comptime block_size = 256

from max.gpu.host import DeviceContext
from std.gpu import block_dim, block_idx, thread_idx
from layout import TileTensor, row_major

comptime vec_layout = row_major[vec_n]()
comptime mat_layout = row_major[mat_n, mat_n]()


def vec_add_kernel(
    lhs: TileTensor[float_dtype, type_of(vec_layout), MutAnyOrigin],
    rhs: TileTensor[float_dtype, type_of(vec_layout), MutAnyOrigin],
    out_t: TileTensor[float_dtype, type_of(vec_layout), MutAnyOrigin],
):
    var tid = block_idx.x * block_dim.x + thread_idx.x
    if tid < vec_n:
        out_t[tid] = lhs[tid] + rhs[tid]


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


def gpu_report() raises -> String:
    comptime if not has_accelerator():
        return String(
            "gpu: not available (built without accelerator support)\n"
            + "hint: this build targets CPU-only hosts; deploy to a GPU device\n"
        )
    else:
        var r = String("")
        var ctx = DeviceContext()
        r += "device: " + ctx.name() + "\n"
        r += "api: " + ctx.api() + "\n"

        # --- vector add: correctness + first-launch vs steady-state latency ---
        var lhs_host = ctx.enqueue_create_host_buffer[float_dtype](vec_n)
        var rhs_host = ctx.enqueue_create_host_buffer[float_dtype](vec_n)
        ctx.synchronize()
        for i in range(vec_n):
            lhs_host[i] = Float32(i)
            rhs_host[i] = Float32(2 * i)
        var lhs_dev = ctx.enqueue_create_buffer[float_dtype](vec_n)
        var rhs_dev = ctx.enqueue_create_buffer[float_dtype](vec_n)
        var out_dev = ctx.enqueue_create_buffer[float_dtype](vec_n)
        ctx.enqueue_copy(dst_buf=lhs_dev, src_buf=lhs_host)
        ctx.enqueue_copy(dst_buf=rhs_dev, src_buf=rhs_host)
        var lhs_t = TileTensor(lhs_dev, vec_layout)
        var rhs_t = TileTensor(rhs_dev, vec_layout)
        var out_t = TileTensor(out_dev, vec_layout)

        comptime vec_blocks = ceildiv(vec_n, block_size)
        var t0 = perf_counter_ns()
        ctx.enqueue_function[vec_add_kernel](
            lhs_t, rhs_t, out_t, grid_dim=vec_blocks, block_dim=block_size
        )
        ctx.synchronize()
        var first_us = Float64(perf_counter_ns() - t0) / 1e3
        t0 = perf_counter_ns()
        ctx.enqueue_function[vec_add_kernel](
            lhs_t, rhs_t, out_t, grid_dim=vec_blocks, block_dim=block_size
        )
        ctx.synchronize()
        var steady_us = Float64(perf_counter_ns() - t0) / 1e3

        var out_host = ctx.enqueue_create_host_buffer[float_dtype](vec_n)
        ctx.enqueue_copy(dst_buf=out_host, src_buf=out_dev)
        ctx.synchronize()
        var errors = 0
        for i in range(vec_n):
            if out_host[i] != Float32(3 * i):
                errors += 1
        r += "vector_add: n=" + String(vec_n) + " errors=" + String(errors)
        r += " first_launch_us=" + String(first_us)
        r += " steady_us=" + String(steady_us) + "\n"

        # --- naive matmul: sustained-throughput sanity number ---
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
        comptime mat_blocks = ceildiv(mat_n, tile)
        # warm launch, then timed
        ctx.enqueue_function[matmul_kernel](
            a_t, b_t, c_t, grid_dim=(mat_blocks, mat_blocks), block_dim=(tile, tile)
        )
        ctx.synchronize()
        t0 = perf_counter_ns()
        ctx.enqueue_function[matmul_kernel](
            a_t, b_t, c_t, grid_dim=(mat_blocks, mat_blocks), block_dim=(tile, tile)
        )
        ctx.synchronize()
        var mm_s = Float64(perf_counter_ns() - t0) / 1e9
        var gflops = 2.0 * Float64(mat_n) * Float64(mat_n) * Float64(mat_n) / mm_s / 1e9

        var c_host = ctx.enqueue_create_host_buffer[float_dtype](mat_n * mat_n)
        ctx.enqueue_copy(dst_buf=c_host, src_buf=c_dev)
        ctx.synchronize()
        var mm_errors = 0
        for i in range(mat_n * mat_n):
            if c_host[i] != Float32(mat_n):
                mm_errors += 1
        r += "matmul: n=" + String(mat_n) + " errors=" + String(mm_errors)
        r += " time_s=" + String(mm_s) + " gflops=" + String(gflops) + "\n"
        r += "status: " + ("OK" if errors == 0 and mm_errors == 0 else "FAILED") + "\n"
        return r


# ---------------- minimal HTTP (libc FFI; no stdlib networking) ----------------


def str_bytes(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    return out^


def send_all(fd: c_int, data: List[UInt8]) raises:
    var sent = 0
    while sent < len(data):
        var n = external_call["send", c_ssize_t](
            fd, data.unsafe_ptr().unsafe_offset(sent), len(data) - sent, c_int(0)
        )
        if n <= 0:
            raise Error("send() failed")
        sent += Int(n)


def read_request(fd: c_int) raises -> String:
    var data = List[UInt8]()
    var buf = List[UInt8](unsafe_uninit_length=2048)
    while True:
        var got = external_call["recv", c_ssize_t](
            fd, buf.unsafe_ptr(), 2048, c_int(0)
        )
        if got <= 0:
            raise Error("peer closed")
        for i in range(Int(got)):
            data.append(buf[i])
        var s = String(from_utf8=data)
        if s.find("\r\n\r\n") >= 0:
            return s


def respond(fd: c_int, status: String, body: String) raises:
    var resp: String = (
        "HTTP/1.1 "
        + status
        + "\r\nContent-Type: text/plain\r\nContent-Length: "
        + String(body.byte_length())
        + "\r\nConnection: close\r\n\r\n"
        + body
    )
    send_all(fd, str_bytes(resp))


def main() raises:
    print("gpu-hello: running device diagnostics...")
    var report: String
    try:
        report = gpu_report()
    except e:
        report = "status: FAILED\nerror: " + String(e) + "\n"
    print(report)

    var port = {{.PORT}}
    var fd = external_call["socket", c_int](c_int(2), c_int(1), c_int(0))
    if fd < 0:
        raise Error("socket() failed")
    var one = List[c_int]()
    one.append(1)
    _ = external_call["setsockopt", c_int](
        fd, c_int(1), c_int(2), one.unsafe_ptr(), c_int(4)
    )
    var addr = List[UInt8]()
    for _ in range(16):
        addr.append(0)
    addr[0] = 2  # AF_INET
    addr[2] = UInt8(port >> 8)
    addr[3] = UInt8(port & 0xFF)
    if external_call["bind", c_int](fd, addr.unsafe_ptr(), c_int(16)) != 0:
        raise Error("bind() failed")
    if external_call["listen", c_int](fd, c_int(8)) != 0:
        raise Error("listen() failed")
    print("gpu-hello serving report on :", port)

    while True:
        var peer = List[UInt8](unsafe_uninit_length=16)
        var peer_len = List[c_int]()
        peer_len.append(16)
        var conn = external_call["accept", c_int](
            fd, peer.unsafe_ptr(), peer_len.unsafe_ptr()
        )
        if conn < 0:
            continue
        try:
            var req = read_request(conn)
            var parts = String(req.split("\r\n")[0]).split(" ")
            var path = String(parts[1]) if len(parts) >= 2 else String("/")
            if path == "/health":
                respond(conn, "200 OK", "ok\n")
            else:
                respond(conn, "200 OK", report)
        except:
            pass
        _ = external_call["close", c_int](conn)
