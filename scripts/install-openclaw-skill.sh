#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE_DIR="$PROJECT_DIR/skills/host-coding-agent"
OPENCLAW_BIN=${OPENCLAW_BIN:-openclaw}

usage() {
  echo "Usage: $0 [--global]" >&2
}

GLOBAL=false
case ${1:-} in
  "") ;;
  --global)
    GLOBAL=true
    shift
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage
    exit 2
    ;;
esac

if [ "$#" -ne 0 ]; then
  usage
  exit 2
fi
if [ ! -f "$SOURCE_DIR/SKILL.md" ]; then
  echo "Skill source not found: $SOURCE_DIR/SKILL.md" >&2
  exit 1
fi
if ! command -v "$OPENCLAW_BIN" >/dev/null 2>&1; then
  echo "OpenClaw CLI not found: $OPENCLAW_BIN" >&2
  exit 1
fi

if [ "$GLOBAL" = true ]; then
  "$OPENCLAW_BIN" skills install "$SOURCE_DIR" --as host-coding-agent --global
  echo "Installed host-coding-agent skill globally for OpenClaw."
else
  "$OPENCLAW_BIN" skills install "$SOURCE_DIR" --as host-coding-agent
  echo "Installed host-coding-agent skill for the current OpenClaw project."
fi
