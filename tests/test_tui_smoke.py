"""Headless TUI smoke (layer 3): mount CCApp on a TEMP state with every node type, expand the whole
tree, and highlight each node — asserting the detail pane NEVER shows a render error. This catches the
"one bad node crashes the whole TUI" class (e.g. the KeyError 'kind' that took the app down).

Run with a python that has `textual` (locally: CC_TEST_PY=~/.cc/venv/bin/python; in CI: pip install textual).
State is redirected to a temp file — the real ~/.cc/state.json is NEVER touched (the clobber lesson).
"""
import asyncio, json, tempfile, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cc, tui
from textual.widgets import Tree, Static


def _make_state(tmp):
    # one of EVERY node type the tree can render: project, epic+task, loose task, (orphans via patch)
    return {
        "projects": {"demo": {"kind": "multi", "path": str(tmp), "default_assignee": "me",
                              "repos": {"web": {"default_branch": "main", "remote": "x/web"},
                                        "api": {"default_branch": "master", "remote": "x/api"}}}},
        "epics": {"E1": {"project": "demo", "summary": "epic one", "mode": "epic_branch",
                         "branch": "E1", "targets": {}, "mrs": {}},
                  "demo__loose": {"project": "demo", "loose": True, "mode": "targets", "targets": {}}},
        "tasks": {
            "t_epic": {"epic": "E1", "title": "epic task", "status": "idle", "branch": "E1-epic-task",
                       "repos": ["web", "api"], "base": {"web": "E1", "api": "E1"}, "worktrees": {},
                       "mrs": {}, "log": ""},
            "t_loose": {"epic": "demo__loose", "title": "loose task", "status": "review", "branch": "loose-task",
                        "repos": ["web"], "base": {"web": "main"}, "worktrees": {}, "mrs": {},
                        "log": "", "needs_input": "какой порт?", "skipped": {"api": "not a git repo"}},
        },
    }


def _walk(node):
    out = []
    for ch in node.children:
        out.append(ch)
        out.extend(_walk(ch))
    return out


async def _smoke():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cc-smoke-"))
    saved = (cc.STATE_FILE, cc._BACKUP_DIR, cc._scan_orphans, cc.AUDIT_FILE)
    cc.STATE_FILE = tmp / "state.json"
    cc._BACKUP_DIR = tmp / "backups"
    cc.AUDIT_FILE = tmp / "audit.log"           # never touch the real ~/.cc/audit.log
    cc.STATE_FILE.write_text(json.dumps(_make_state(tmp)))
    cc.audit("task.add", task="t_loose", epic="demo__loose", repos=["web"])  # so the detail's timeline block renders
    cc.audit("task.merge", task="t_loose", repo="web", mr=7, base="main")
    # force the "⚠️ Потеряшки" node to render too, without touching the real fs
    cc._scan_orphans = lambda s: [{"project": "demo", "epic": "E1", "slug": "ghost",
                                   "branch": "E1-ghost", "repos": {"web": "/x"}}]
    try:
        app = tui.CCApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#tree", Tree)
            for n in _walk(tree.root):          # expand the whole tree
                try:
                    n.expand()
                except Exception:
                    pass
            await pilot.pause()
            detail = app.query_one("#detail", Static)
            errors, visited, saw_timeline = [], 0, False
            for n in _walk(tree.root):
                if not n.data:
                    continue
                visited += 1
                tree.move_cursor(n)             # real path -> on_tree_node_highlighted -> show_detail
                await pilot.pause()
                txt = str(detail.render())
                if "ошибка отрисовки" in txt:   # the guard's fallback == show_detail threw
                    errors.append((n.data, txt[:120]))
                if n.data.get("type") == "task" and n.data.get("id") == "t_loose" and "timeline:" in txt:
                    saw_timeline = True         # per-task audit block rendered from the audit log
            assert visited >= 5, "expected to visit project/epic/2 tasks/orphans, got %d" % visited
            assert not errors, "detail render errors:\n" + "\n".join(str(e) for e in errors)
            assert saw_timeline, "task detail should show the audit timeline block"
            # the legend must be present (its absence == compose regression)
            assert "ждёт тебя" in str(app.query_one("#legend", Static).render())
            print("TUI SMOKE OK: %d nodes rendered, 0 errors" % visited)
        # the temp state must still be intact (no clobber)
        assert cc._valid_state(json.loads(cc.STATE_FILE.read_text()))
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR, cc._scan_orphans, cc.AUDIT_FILE = saved
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(_smoke())
    print("ok test_tui_smoke")
