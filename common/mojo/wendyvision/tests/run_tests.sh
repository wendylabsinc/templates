#!/bin/sh
# wendyvision test harness. Run inside a linux/arm64 container with mojo +
# pillow + libturbojpeg0:
#   docker run --rm -v $(dirname of common/mojo):/pkgs python-starter-max:latest \
#     sh /pkgs/wendyvision/tests/run_tests.sh /pkgs
set -eu
PKGS="${1:-/pkgs}"
TESTS="$PKGS/wendyvision/tests"

if [ ! -e /usr/lib/*/libturbojpeg.so.0 ] && [ ! -e /usr/lib/libturbojpeg.so.0 ]; then
    apt-get update -qq
    # Debian names it libturbojpeg0, Ubuntu libturbojpeg.
    apt-get install -y -qq libturbojpeg0 >/dev/null 2>&1 \
        || apt-get install -y -qq libturbojpeg >/dev/null
fi

mkdir -p /tmp/wendyvision
python3 "$TESTS/gen_oracle.py" /tmp/wendyvision

cd "$PKGS"
mojo run -I "$PKGS" "$TESTS/test_vision.mojo"
