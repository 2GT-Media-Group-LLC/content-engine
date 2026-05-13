#!/usr/bin/env bash
# Install content-engine LaunchAgents from templates. Idempotent.
#
# Templates live in scripts/templates/*.plist.tmpl with placeholders
# (__PROJECT_ROOT__, __LABEL__, __GUI_PORT__) that get substituted on install.
# Generated plists land in ~/Library/LaunchAgents — gitignored, never tracked.
#
# Usage:
#   ./scripts/install-launchd.sh                # install/reload both agents
#   ./scripts/install-launchd.sh gui            # only the GUI agent
#   ./scripts/install-launchd.sh weekly         # only the weekly cron
#
# Optional environment:
#   LABEL_PREFIX (default: com.contentengine)
#   GUI_PORT     (default: 8080)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DEST_DIR="$HOME/Library/LaunchAgents"
TPL_DIR="$SCRIPT_DIR/templates"

LABEL_PREFIX="${LABEL_PREFIX:-com.contentengine}"
GUI_PORT="${GUI_PORT:-8080}"

mkdir -p "$DEST_DIR" "$ROOT/data/runs"

# Each candidate: <suffix>:<template>:<description>
CANDIDATES=(
  "weekly:com.contentengine.weekly.plist.tmpl:weekly cycle, Sunday 3am"
  "gui:com.contentengine.gui.plist.tmpl:GUI on http://127.0.0.1:${GUI_PORT}"
)

filter="${1:-}"

render_template() {
  local src="$1" dest="$2" label="$3"
  /usr/bin/sed \
    -e "s|__PROJECT_ROOT__|${ROOT}|g" \
    -e "s|__LABEL__|${label}|g" \
    -e "s|__GUI_PORT__|${GUI_PORT}|g" \
    "$src" > "$dest"
}

installed=0
for entry in "${CANDIDATES[@]}"; do
  suffix="${entry%%:*}"
  rest="${entry#*:}"
  tpl_name="${rest%%:*}"
  desc="${rest##*:}"
  src="${TPL_DIR}/${tpl_name}"
  label="${LABEL_PREFIX}.${suffix}"
  dest="${DEST_DIR}/${label}.plist"

  if [ -n "$filter" ] && [[ "$suffix" != *"$filter"* ]]; then
    continue
  fi
  if [ ! -f "$src" ]; then
    echo "  ✗ missing template: $src" >&2
    continue
  fi

  # Unload existing (any name matching old or new label prefix).
  for existing in "$DEST_DIR"/*."${suffix}.plist"; do
    [ -e "$existing" ] || continue
    if launchctl list 2>/dev/null | grep -q "$(basename "$existing" .plist)"; then
      echo "  → unloading existing $(basename "$existing")"
      launchctl unload "$existing" 2>/dev/null || true
    fi
  done

  render_template "$src" "$dest" "$label"
  launchctl load "$dest"
  echo "  ✓ ${label}  ($desc)"
  installed=$((installed + 1))
done

if [ "$installed" -eq 0 ]; then
  echo "Nothing installed. Filter '$filter' didn't match." >&2
  exit 1
fi

echo
echo "Project root: $ROOT"
echo "Label prefix: $LABEL_PREFIX"
echo
echo "Useful commands:"
echo "  launchctl list | grep contentengine"
echo "  launchctl start ${LABEL_PREFIX}.weekly        # trigger a run now"
echo "  tail -f $ROOT/data/runs/{gui,weekly}.{out,err}.log"
echo "  launchctl unload $DEST_DIR/${LABEL_PREFIX}.gui.plist     # stop GUI"
