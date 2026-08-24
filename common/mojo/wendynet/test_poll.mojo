# PollSet behavior test using a socketpair: readable only after a send, and a
# timed-out poll on an idle socket reports nothing ready. (A socketpair
# instead of a pipe because the stdlib already declares the `write` extern
# with a different FFI signature, and duplicate declarations collide.)
from std.ffi import external_call, c_int

from wendynet.net import send_all
from wendynet.poll import PollSet, POLLIN


def main() raises:
    var fds = List[c_int]()
    fds.append(0)
    fds.append(0)
    # socketpair(AF_UNIX=1, SOCK_STREAM=1, 0, fds)
    if (
        external_call["socketpair", c_int](
            c_int(1), c_int(1), c_int(0), fds.unsafe_ptr()
        )
        != 0
    ):
        raise Error("socketpair() failed")
    var rfd = fds[0]
    var wfd = fds[1]

    var ps = PollSet()
    ps.add(rfd, POLLIN)
    if ps.poll(50) != 0:
        raise Error("idle socket reported ready")
    print("PASS: idle socket times out with nothing ready")

    var one = List[UInt8]()
    one.append(0x42)
    send_all(wfd, one)
    var ready = ps.poll(1000)
    if ready != 1:
        raise Error("expected 1 ready fd, got " + String(ready))
    if (ps.revents(0) & POLLIN) == 0:
        raise Error("POLLIN not set after send")
    print("PASS: socket readable after send")

    _ = external_call["close", c_int](rfd)
    _ = external_call["close", c_int](wfd)
