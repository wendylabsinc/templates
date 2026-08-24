#!/bin/sh
# wendycam test harness. Run inside a linux/arm64 container with gcc + mojo:
#   docker run --rm -v $(dirname of common/mojo):/pkgs python-starter-max:latest \
#     sh /pkgs/wendycam/tests/run_tests.sh /pkgs
# Diffs the kernel-header ABI oracle (abi_ref.c) against wendycam.v4l2's view.
set -eu
PKGS="${1:-/pkgs}"
TESTS="$PKGS/wendycam/tests"

gcc -o /tmp/abi_ref "$TESTS/abi_ref.c"
/tmp/abi_ref > /tmp/abi_ref.txt

cd "$PKGS"
mojo run -I "$PKGS" "$TESTS/test_v4l2_abi.mojo" > /tmp/abi_mojo.txt

if diff -u /tmp/abi_ref.txt /tmp/abi_mojo.txt; then
    echo "PASS: wendycam.v4l2 ABI matches kernel headers"
else
    echo "FAIL: wendycam.v4l2 ABI diverges from kernel headers" >&2
    exit 1
fi

mojo run -I "$PKGS" "$TESTS/test_camera_nocam.mojo"
