#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LABEL="com.jaehyunlee.host-coding-agent-mcp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UV_BIN=$(command -v uv)
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__UV_BIN__|$UV_BIN|g" \
  "$PROJECT_DIR/scripts/launchd.plist.template" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST"
echo "Status: launchctl print gui/$(id -u)/$LABEL"
