#!/bin/sh
set -eu

REPO=/Users/pesh/Sites/zotero-audio
PLIST="$HOME/Library/LaunchAgents/com.pesh.zotero-audio.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p /Users/pesh/Sites/zotero-audio-runtime/full-library
cp "$REPO/launchd/com.pesh.zotero-audio.plist" "$PLIST"
launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/com.pesh.zotero-audio"
echo "Installed and started $PLIST"
