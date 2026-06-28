#!/bin/sh
set -eu

SECRETS_FILE=${HOST_CODING_AGENT_SECRETS_FILE:-"$HOME/.config/host-coding-agent-mcp/tokens.env"}

mkdir -p "$(dirname "$SECRETS_FILE")"
umask 077
touch "$SECRETS_FILE"

ensure_token() {
  key=$1
  if grep -q "^export ${key}=" "$SECRETS_FILE"; then
    echo "Token already exists: $key"
    return
  fi
  token=$(openssl rand -hex 32)
  printf "export %s='%s'\n" "$key" "$token" >> "$SECRETS_FILE"
  echo "Created token: $key"
}

ensure_token HOST_CODING_AGENT_DEV_BOT_TOKEN
ensure_token HOST_CODING_AGENT_INVEST_BOT_TOKEN
ensure_token HOST_CODING_AGENT_RESEARCH_BOT_TOKEN
ensure_token HOST_CODING_AGENT_YOUTUBE_BOT_TOKEN

echo "Bearer token file: $SECRETS_FILE"
echo "Keep this file private and copy each token only into its matching MCP profile."
