#!/usr/bin/env bash
# Run the webcam app locally on macOS using uv.
# Requires: brew install gstreamer pygobject3
set -euo pipefail

export DYLD_LIBRARY_PATH="/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="/opt/homebrew/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GST_PLUGIN_PATH="/opt/homebrew/lib/gstreamer-1.0${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"

exec uv run --python 3.13 python app.py
