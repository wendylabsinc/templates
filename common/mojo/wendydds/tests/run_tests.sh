#!/bin/sh
# wendydds test harness. Run inside a linux/arm64 container with mojo AND
# CycloneDDS 0.10.5 (headers + libddsc.so.0), e.g. the local mojo-dds-spike
# image (python-starter-max + CycloneDDS built from source):
#   docker run --rm -v $(dir of common/mojo):/pkgs mojo-dds-spike \
#     sh /pkgs/wendydds/tests/run_tests.sh /pkgs
# The C oracle is compiled against the real headers and its output is
# asserted by the Mojo suite (same pattern as wendycam's V4L2 ABI tests).
set -eu
PKGS="${1:-/pkgs}"
TESTS="$PKGS/wendydds/tests"

if [ ! -e /usr/local/lib/libddsc.so.0 ]; then
    echo "CycloneDDS 0.10.5 not found at /usr/local/lib — build it first" >&2
    echo "(see the swift/ros2-talker-listener Dockerfile for the recipe)" >&2
    exit 1
fi

gcc "$TESTS/abi_ref.c" -I/usr/local/include -o /tmp/dds_abi_ref
/tmp/dds_abi_ref > /tmp/dds_abi.txt

DDS_ABI_REF=/tmp/dds_abi.txt LD_LIBRARY_PATH=/usr/local/lib \
    mojo run -I "$PKGS" "$TESTS/test_wendydds.mojo"
