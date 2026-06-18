#!/usr/bin/env bash
# cc launcher: `cc tui` runs the Textual TUI inside tmux session "cc" (window "tui"),
# so task sessions are sibling tmux WINDOWS you switch to with `o` and return via Ctrl-B 0.
CC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.cc/venv/bin/python"
if [ "$1" = "tui" ]; then
  TUI_CMD="$VENV $CC_DIR/tui.py"
  if [ -n "$TMUX" ]; then
    exec $TUI_CMD                                   # already inside tmux
  fi
  if command -v tmux >/dev/null 2>&1 && tmux has-session -t cc 2>/dev/null; then
    tmux list-windows -t cc -F '#{window_name}' 2>/dev/null | grep -qx tui \
      || tmux new-window -t cc -n tui "$TUI_CMD"
    tmux select-window -t cc:tui 2>/dev/null
    exec tmux attach -t cc
  fi
  if command -v tmux >/dev/null 2>&1; then
    exec tmux new-session -s cc -n tui "$TUI_CMD"
  fi
  exec $TUI_CMD                                     # no tmux -> run bare
fi
exec python3 "$CC_DIR/cc.py" "$@"
