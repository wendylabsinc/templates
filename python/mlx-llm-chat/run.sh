#!/usr/bin/env bash
# Run the Qwen3 chat app locally on macOS using uv + mlx-lm (Metal).
# The frontend must be built first: cd frontend && npm ci && npm run build
set -euo pipefail

exec uv run python app.py
