#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SECRETS_FILE=${HOST_CODING_AGENT_SECRETS_FILE:-"$HOME/.config/host-coding-agent-mcp/tokens.env"}
UV_BIN=${UV_BIN:-$(command -v uv)}

if [ -f "$SECRETS_FILE" ]; then
  # File is generated with mode 0600 by scripts/generate-token.sh.
  . "$SECRETS_FILE"
fi

cd "$PROJECT_DIR"

exec "$UV_BIN" run python server.py --config "$PROJECT_DIR/config.yaml"
