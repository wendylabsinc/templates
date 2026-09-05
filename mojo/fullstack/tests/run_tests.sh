#!/bin/sh
# fullstack backend test harness. Run inside a linux/arm64 container with mojo:
#   docker run --rm -v $(repo root)/mojo/fullstack:/tpl python-starter-max:latest \
#     sh /tpl/tests/run_tests.sh /tpl
# Imports resolve against the template dir itself, so this also validates the
# vendored wendydb/wendynet package copies.
set -eu
TPL="${1:-/tpl}"

FIXTURES="$TPL/tests/fixtures" mojo run -I "$TPL" "$TPL/tests/test_fullstack.mojo"
