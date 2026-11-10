#!/bin/sh
# Download the G1 and Dex3 model files into teleop/assets for running the
# tests on your computer. The teleop image fetches the same files itself
# (teleop/Dockerfile), so you only need this for local tests.
set -eu
REF=817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b   # keep in step with teleop/Dockerfile
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/teleop/assets"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone --quiet --filter=blob:none --no-checkout \
  https://github.com/unitreerobotics/xr_teleoperate.git "$TMP/xr"
git -C "$TMP/xr" sparse-checkout set assets/g1 assets/unitree_hand
git -C "$TMP/xr" checkout --quiet "$REF"
rm -rf "$DEST"
mkdir -p "$DEST"
mv "$TMP/xr/assets/g1" "$TMP/xr/assets/unitree_hand" "$DEST/"
cp "$TMP/xr/LICENSE" "$DEST/LICENSE"
echo "G1 and Dex3 assets at $DEST"
