#!/usr/bin/env bash
# Build a standalone macOS .app bundle (no Python install needed on target machine).
# Run once from this directory: chmod +x build_mac.sh && ./build_mac.sh
# Output:
#   dist/AdvisingBot.app
#   dist/AdvisingBot-mac.zip   (single file you can share)
#
# First run on a new machine:
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt
#   ./build_mac.sh

set -euo pipefail

# Build in a clean Python env (Conda vars can break frozen pandas/numpy).
unset _PYTHON_SYSCONFIGDATA_NAME || true
unset PYTHONHOME || true
unset PYTHONPATH || true

# Collect all data files into the bundle
DATA_FLAGS=""
for f in curricula_registry.json AHelectives.csv SSelectives.csv TE_Rules.csv; do
    [ -f "$f" ] && DATA_FLAGS="$DATA_FLAGS --add-data $f:."
done
for f in curriculum_*.csv; do
    [ -f "$f" ] && DATA_FLAGS="$DATA_FLAGS --add-data $f:."
done
for f in *_TE.csv; do
    [ -f "$f" ] && DATA_FLAGS="$DATA_FLAGS --add-data $f:."
done
for f in minor_*.csv; do
    [ -f "$f" ] && DATA_FLAGS="$DATA_FLAGS --add-data $f:."
done

# shellcheck disable=SC2086
pyinstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "AdvisingBot" \
    --runtime-hook pyi_runtime_env.py \
    --hidden-import _sysconfigdata__darwin_darwin \
    --hidden-import cmath \
    --hidden-import pandas._libs.testing \
    --hidden-import pandas._libs.tslibs.base \
    --hidden-import numpy._core._exceptions \
    --exclude-module numpy.tests \
    --exclude-module pandas.tests \
    $DATA_FLAGS \
    AdvisingBot.py

# Zip the .app as a single sharable file
rm -f dist/AdvisingBot-mac.zip
ditto -c -k --sequesterRsrc --keepParent dist/AdvisingBot.app dist/AdvisingBot-mac.zip

echo ""
echo "Build complete:"
echo "  dist/AdvisingBot.app"
echo "  dist/AdvisingBot-mac.zip"
echo "First run on a new Mac: right-click → Open (to bypass Gatekeeper warning)"
