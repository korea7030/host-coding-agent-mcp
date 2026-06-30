#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE_DIR="$PROJECT_DIR/hermes_plugins/development-policy"
CONTAINER=${1:-hermes-dev}
PROFILE=${2:-dev-bot}
PROFILE_HOME="/opt/data/profiles/$PROFILE"
DEST_DIR="$PROFILE_HOME/plugins/development-policy"

docker exec --user hermes "$CONTAINER" mkdir -p "$DEST_DIR"
docker cp "$SOURCE_DIR/." "$CONTAINER:$DEST_DIR/"
docker exec --user root "$CONTAINER" chown -R hermes:hermes "$DEST_DIR"

docker exec --user hermes "$CONTAINER" sh -lc \
  "HERMES_HOME=$PROFILE_HOME HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins enable development-policy"

docker exec -i --user hermes "$CONTAINER" sh -lc \
  "HERMES_HOME=$PROFILE_HOME HOME=/opt/data /opt/hermes/.venv/bin/python - '$PROFILE_HOME/SOUL.md' '$DEST_DIR/SOUL_APPEND.md'" <<'PY'
from pathlib import Path
import sys

soul_path = Path(sys.argv[1])
append_path = Path(sys.argv[2])
start = "<!-- host-coding-agent-policy:start -->"
end = "<!-- host-coding-agent-policy:end -->"
block = f"{start}\n{append_path.read_text().strip()}\n{end}"
current = soul_path.read_text() if soul_path.exists() else ""
if start in current and end in current:
    prefix, remainder = current.split(start, 1)
    _, suffix = remainder.split(end, 1)
    updated = f"{prefix.rstrip()}\n\n{block}{suffix}"
else:
    updated = f"{current.rstrip()}\n\n{block}\n"
soul_path.write_text(updated)
PY

echo "Installed development-policy in $CONTAINER/$PROFILE"
echo "Restart the profile gateway to load the plugin."
