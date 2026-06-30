#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LABEL="com.jaehyunlee.host-coding-agent-mcp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="gui/$(id -u)/$LABEL"
UV_BIN=$(command -v uv)
NODE_BIN_DIR=$(dirname "$(command -v node)")
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__UV_BIN__|$UV_BIN|g" \
  -e "s|__NODE_BIN_DIR__|$NODE_BIN_DIR|g" \
  "$PROJECT_DIR/scripts/launchd.plist.template" > "$PLIST"

launchctl bootout "$TARGET" 2>/dev/null || \
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true

# launchd may take a moment to fully remove a KeepAlive job.
attempt=0
while launchctl print "$TARGET" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 10 ]; then
    echo "Timed out waiting for existing launchd job to stop: $TARGET" >&2
    exit 1
  fi
  sleep 1
done

launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "$TARGET"

echo "Installed: $PLIST"
echo "Status: launchctl print $TARGET"
