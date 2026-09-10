#!/bin/sh
set -eu
APP_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_RUNTIME=${ZOTERO_AUDIO_RUNTIME:-"$HOME/Sites/zotero-audio-runtime"}
cd "$APP_REPO"
"$APP_RUNTIME/venv/bin/python" -m pytest
npm --prefix cloud run check
npm --prefix cloud test
node --check cloud/public/app.js
