#!/bin/sh
# wendydb test harness. Run inside a linux/arm64 container with mojo:
#   docker run --rm -v $(dir of common/mojo):/pkgs python-starter-max:latest \
#     sh /pkgs/wendydb/tests/run_tests.sh /pkgs
# Uses the distro libsqlite3.so.0 already present in the image.
set -eu
PKGS="${1:-/pkgs}"
TESTS="$PKGS/wendydb/tests"

mojo run -I "$PKGS" "$TESTS/test_wendydb.mojo"
