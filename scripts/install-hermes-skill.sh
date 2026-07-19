#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE_FILE="$PROJECT_DIR/skills/host-coding-agent/SKILL.md"
BASE_HOME=${HERMES_HOME:-"$HOME/.hermes"}
PROFILE=

usage() {
  cat >&2 <<EOF
Usage: $0 [--profile NAME] [--hermes-home DIR]

Without --profile, installs into HERMES_HOME/skills (default: ~/.hermes/skills).
With --profile, installs into HERMES_HOME/profiles/NAME/skills.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -n "$2" ] || { echo "Hermes profile name must not be empty" >&2; exit 2; }
      PROFILE=$2
      shift 2
      ;;
    --hermes-home)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -n "$2" ] || { echo "Hermes home must not be empty" >&2; exit 2; }
      BASE_HOME=$2
      shift 2
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
done

if [ ! -f "$SOURCE_FILE" ]; then
  echo "Skill source not found: $SOURCE_FILE" >&2
  exit 1
fi
if [ -n "$PROFILE" ]; then
  case "$PROFILE" in
    *[!A-Za-z0-9._-]*|.|..)
      echo "Invalid Hermes profile name: $PROFILE" >&2
      exit 2
      ;;
  esac
  PROFILE_HOME="$BASE_HOME/profiles/$PROFILE"
else
  PROFILE_HOME=$BASE_HOME
fi

SKILLS_DIR="$PROFILE_HOME/skills"
DEST_DIR="$SKILLS_DIR/host-coding-agent"
DEST_FILE="$DEST_DIR/SKILL.md"

if [ -L "$SKILLS_DIR" ] || [ -L "$DEST_DIR" ] || [ -L "$DEST_FILE" ]; then
  echo "Refusing to install through a symbolic link: $DEST_FILE" >&2
  exit 1
fi

umask 022
mkdir -p "$DEST_DIR"
TMP_FILE=$(mktemp "$DEST_DIR/.SKILL.md.tmp.XXXXXX")
trap 'rm -f "$TMP_FILE"' EXIT HUP INT TERM
cp "$SOURCE_FILE" "$TMP_FILE"
chmod 0644 "$TMP_FILE"
mv -f "$TMP_FILE" "$DEST_FILE"
trap - EXIT HUP INT TERM

if [ -n "$PROFILE" ]; then
  echo "Installed host-coding-agent skill for Hermes profile '$PROFILE': $DEST_FILE"
else
  echo "Installed host-coding-agent skill for Hermes: $DEST_FILE"
fi
