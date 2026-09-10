#!/bin/sh
# Refresh the editable local app and restart its worker, keeping all model caches.
set -eu
APP_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_RUNTIME=${ZOTERO_AUDIO_RUNTIME:-"$HOME/Sites/zotero-audio-runtime"}
APP_PYTHON="$APP_RUNTIME/venv/bin/python"
if [ ! -x "$APP_PYTHON" ]; then
  echo "Create a Python 3.11+ virtual environment at $APP_RUNTIME/venv first."
  exit 1
fi
"$APP_PYTHON" -m pip install --quiet --no-deps -e "$APP_REPO"
"$APP_PYTHON" -m zotero_audio.app_cli restart
