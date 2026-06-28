#!/bin/sh
set -eu

SECRETS_FILE=${HOST_CODING_AGENT_SECRETS_FILE:-"$HOME/.config/host-coding-agent-mcp/tokens.env"}

if [ -e "$SECRETS_FILE" ]; then
  echo "Refusing to overwrite existing secrets file: $SECRETS_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$SECRETS_FILE")"
umask 077
TOKEN=$(openssl rand -hex 32)
printf "export HOST_CODING_AGENT_DEV_BOT_TOKEN='%s'\n" "$TOKEN" > "$SECRETS_FILE"

echo "Created bearer token file: $SECRETS_FILE"
echo "Keep this file private and copy the token only into the dev-bot MCP client configuration."
