#!/bin/sh
# wendyaudio test harness. Run inside a linux/arm64 container with mojo:
#   docker run --rm -v $(dir of common/mojo):/pkgs python-starter-max:latest \
#     sh /pkgs/wendyaudio/tests/run_tests.sh /pkgs
# Installs libasound2 (null-device capture needs the real library, not
# hardware), then runs the Mojo test suite.
set -eu
PKGS="${1:-/pkgs}"
TESTS="$PKGS/wendyaudio/tests"

if [ ! -e /usr/lib/aarch64-linux-gnu/libasound.so.2 ] \
   && [ ! -e /usr/lib/x86_64-linux-gnu/libasound.so.2 ]; then
    apt-get update -qq >/dev/null && apt-get install -y -qq libasound2 >/dev/null
fi

FIXTURES="$TESTS/fixtures" mojo run -I "$PKGS" "$TESTS/test_wendyaudio.mojo"
