#!/bin/bash
# Build "Home Duo Recovery.app" on a Mac (Apple Silicon or Intel): ./build_macos.sh
# Reading the raw disk asks for an administrator password via macOS authopen at runtime.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt pyinstaller
.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name "Home Duo Recovery" \
  --osx-bundle-identifier com.local.homeduo-recovery \
  HomeDuoRecovery.py
(cd dist && ditto -c -k --keepParent "Home Duo Recovery.app" HomeDuoRecovery-mac.zip)
echo "Built: dist/HomeDuoRecovery-mac.zip (give this file to users)"
