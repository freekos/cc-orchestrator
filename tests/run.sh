#!/usr/bin/env bash
# Full cc test suite. Unit tests need only python3; the TUI smoke needs `textual`.
# Local:  CC_TEST_PY=~/.cc/venv/bin/python tests/run.sh      (venv that has textual)
# CI:     textual is pip-installed, so plain python3 works.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${CC_TEST_PY:-python3}"
echo "== unit (test_cc.py) =="
"$PY" "$DIR/test_cc.py"
echo "== TUI smoke (tests/test_tui_smoke.py) =="
"$PY" "$DIR/tests/test_tui_smoke.py"
echo "ALL TESTS PASSED ✓"
