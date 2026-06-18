#!/usr/bin/env bash
# cc launcher: `cc tui` -> Textual TUI (venv); everything else -> engine
CC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.cc/venv/bin/python"
if [ "$1" = "tui" ]; then
  exec "$VENV" "$CC_DIR/tui.py"
fi
exec python3 "$CC_DIR/cc.py" "$@"
