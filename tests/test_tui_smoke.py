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
        "ops": {                                   # an ops-agent run under E1 (must render + detail cleanly)
            "ops_test_e1": {"epic": "E1", "kind": "test", "dir": str(tmp), "log": str(tmp / "ops.log"),
                            "status": "running"},
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
            errors, visited, saw_timeline, saw_ops = [], 0, False, False
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
                if n.data.get("type") == "ops" and "ops: test" in txt:
                    saw_ops = True              # ops run rendered + its detail panel works
            assert visited >= 6, "expected project/epic/2 tasks/ops/orphans, got %d" % visited
            assert not errors, "detail render errors:\n" + "\n".join(str(e) for e in errors)
            assert saw_timeline, "task detail should show the audit timeline block"
            assert saw_ops, "ops run should render in the tree with a working detail panel"
            # the legend must be present (its absence == compose regression)
            assert "ждёт тебя" in str(app.query_one("#legend", Static).render())
            print("TUI SMOKE OK: %d nodes rendered, 0 errors" % visited)
        # the temp state must still be intact (no clobber)
        assert cc._valid_state(json.loads(cc.STATE_FILE.read_text()))
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR, cc._scan_orphans, cc.AUDIT_FILE = saved
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_parse_mr_plan():
    # task MR dry-run + epic MR dry-run + skipped lines -> only the real targets, prod-flagged
    out = "\n".join([
        "[web] DRY-RUN: fix-login -> master | title='x' reviewer=- assignee=me label=E",
        "[api] DRY-RUN: fix-login -> E1-integration | title='x' reviewer=- assignee=me label=E",
        "[mob] no changes vs master - skipped",
        "[svc] DRY-RUN epic MR: E1 -> main  (3 commit(s) ahead, reviewer lead)",
    ])
    rows = tui._parse_mr_plan(out)
    assert ("web", "fix-login", "master", True) in rows          # task -> master = PROD
    assert ("api", "fix-login", "E1-integration", False) in rows # task -> integration = not prod
    assert ("svc", "E1", "main", True) in rows                   # epic->main = release (prod)
    assert all(r[0] != "mob" for r in rows)                      # skipped repo absent
    assert len(rows) == 3
    assert tui._parse_mr_plan("nothing here\n[x] no changes vs main - skipped") == []


async def _mrconfirm():
    # MRConfirmScreen renders the plan, flags PROD, and only confirms (->True) with rows present.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cc-mrc-"))
    saved = (cc.STATE_FILE, cc._BACKUP_DIR, cc.AUDIT_FILE)
    cc.STATE_FILE = tmp / "state.json"; cc._BACKUP_DIR = tmp / "backups"; cc.AUDIT_FILE = tmp / "audit.log"
    cc.STATE_FILE.write_text(json.dumps(_make_state(tmp)))
    try:
        app = tui.CCApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            result = {}
            scr = tui.MRConfirmScreen("Создать MR — test", ["true"])
            scr._compute = lambda: None                       # don't spawn the dry-run subprocess
            app.push_screen(scr, lambda r: result.__setitem__("r", r))
            await pilot.pause()
            scr._show_plan([("web", "fix", "master", True), ("api", "fix", "E1-int", False)])
            await pilot.pause()
            txt = str(scr.query_one("#mrc", Static).render())
            assert "PROD" in txt and "СОЗДАТЬ" in txt, txt
            scr.action_confirm()
            await pilot.pause()
            assert result.get("r") is True, result            # rows present -> confirm yields True
        # empty plan must NOT confirm (guards an accidental create when nothing changed)
        app2 = tui.CCApp()
        async with app2.run_test() as pilot:
            await pilot.pause()
            res2 = {}
            scr2 = tui.MRConfirmScreen("x", ["true"]); scr2._compute = lambda: None
            app2.push_screen(scr2, lambda r: res2.__setitem__("r", r))
            await pilot.pause()
            scr2._show_plan([])
            scr2.action_confirm()
            await pilot.pause()
            assert res2.get("r") is False, res2
        print("MRCONFIRM OK")
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR, cc.AUDIT_FILE = saved
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_group_options():
    s = {"projects": {"P": {}, "Q": {}},
         "epics": {"A": {"project": "P", "summary": "aaa"}, "B": {"project": "P", "summary": "bbb"},
                   "C": {"project": "P", "summary": "ccc", "archived": True},
                   "P__loose": {"project": "P", "loose": True},
                   "Z": {"project": "Q", "summary": "zzz"}},
         "tasks": {"t": {"epic": "A"}, "t2": {"epic": "P__loose"}}}
    opts, cur = tui._group_options(s, "t")
    vals = [v for _, v in opts]
    assert cur == "A"
    assert "B" in vals and "A" not in vals           # other epic offered, current excluded
    assert "C" not in vals and "Z" not in vals        # archived + other-project excluded
    assert "P__loose" not in vals and "P" in vals     # loose container not a named group; ungroup offered
    opts2, _ = tui._group_options(s, "t2")            # already loose -> no ungroup option
    vals2 = [v for _, v in opts2]
    assert "P" not in vals2 and "A" in vals2 and "B" in vals2


async def _movescreen():
    # MoveToGroupScreen mounts and renders without error; cancel -> None (regression guard)
    app = tui.CCApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        res = {}
        app.push_screen(tui.MoveToGroupScreen("t_x", [("B — bbb", "B"), ("(без группы)", "P")], "A"),
                        lambda r: res.__setitem__("r", r))
        await pilot.pause()
        assert isinstance(app.screen, tui.MoveToGroupScreen)
        app.screen.action_cancel()
        await pilot.pause()
        assert res.get("r") is None
    print("MOVESCREEN OK")


def test_group_panel_rows():
    s = {"projects": {"P": {"repos": {}}},
         "epics": {"E1": {"project": "P", "summary": "epic one", "mode": "epic_branch"}},
         "tasks": {
             "t_a": {"epic": "E1", "title": "AA", "status": "mr", "repos": ["web"],
                     "base": {"web": "E1"}, "mrs": {"web": "http://mr/1"}},
             "t_b": {"epic": "E1", "title": "BB", "status": "merged", "repos": ["web", "api"],
                     "base": {"web": "E1", "api": "E1"}, "mrs": {"web": "http://mr/2"}, "merged": True},
             "t_other": {"epic": "OTHER", "title": "X", "repos": []},
         }}
    epic, rows = tui._group_panel_rows(s, "E1")
    assert epic["summary"] == "epic one"
    tids = [r["tid"] for r in rows]
    assert tids == ["t_a", "t_b"]                       # only E1's tasks, un-merged first
    a = rows[0]
    assert a["repos"] == [("web", "http://mr/1", "E1")] and not a["merged"]
    b = rows[1]
    assert b["merged"] and ("api", None, "E1") in b["repos"]   # api has no MR -> None
    assert tui._group_panel_rows(s, "EMPTY") == ({}, [])


class _Ev:
    def __init__(self, bid):
        self.button = type("B", (), {"id": bid})()


async def _panelscreen():
    # GroupPanelScreen mounts, renders task rows + MR state; Release button returns the action;
    # loose group has NO Release button.
    epic = {"summary": "epic one", "mode": "epic_branch"}
    rows = [{"tid": "t_a", "title": "AA", "status": "mr", "merged": False,
             "repos": [("web", "http://mr/1", "E1")]},
            {"tid": "t_b", "title": "BB", "status": "merged", "merged": True,
             "repos": [("api", None, "E1")]}]
    app = tui.CCApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        res = {}
        app.push_screen(tui.GroupPanelScreen("E1", epic, rows), lambda r: res.__setitem__("r", r))
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, tui.GroupPanelScreen)
        txt = str(scr.query_one("#pc", Static).render())
        assert "AA" in txt and "MR открыт" in txt and "влит" in txt and "итог: 2 задач, 1 влито" in txt, txt
        assert scr._urls == ["http://mr/1"]              # only real MR urls collected
        assert len(scr.query("#release_mr")) == 1        # epic group -> Release button present
        assert len(scr.query("#merge_tasks")) == 1       # ...and the "влить задачи" button
        assert len(scr.query("#ops_test")) == 1 and len(scr.query("#ops_stage")) == 1  # ops agent buttons
        scr.on_button_pressed(_Ev("ops_test"))           # press Test (агент)
        await pilot.pause()
        assert res.get("r") == {"action": "ops", "kind": "test"}, res
    # loose group -> no release/merge, but Test/Stage ops buttons still available
    app2 = tui.CCApp()
    async with app2.run_test() as pilot:
        await pilot.pause()
        r2 = {}
        app2.push_screen(tui.GroupPanelScreen("P__loose", {"loose": True}, []),
                         lambda r: r2.__setitem__("r", r))
        await pilot.pause()
        s2 = app2.screen
        assert len(s2.query("#release_mr")) == 0 and len(s2.query("#merge_tasks")) == 0
        assert len(s2.query("#ops_test")) == 1            # ops still offered for loose groups
        s2.on_button_pressed(_Ev("ops_stage"))
        await pilot.pause()
        assert r2.get("r") == {"action": "ops", "kind": "stage"}, r2
    print("PANELSCREEN OK")


async def _confirmscreen():
    # generic ConfirmScreen: ok -> True, cancel -> False
    app = tui.CCApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        r1 = {}
        app.push_screen(tui.ConfirmScreen("t", "msg", ok_label="Влить"), lambda r: r1.__setitem__("r", r))
        await pilot.pause()
        app.screen.action_ok()
        await pilot.pause()
        assert r1.get("r") is True, r1
        r2 = {}
        app.push_screen(tui.ConfirmScreen("t", "msg"), lambda r: r2.__setitem__("r", r))
        await pilot.pause()
        app.screen.action_cancel()
        await pilot.pause()
        assert r2.get("r") is False, r2
    print("CONFIRMSCREEN OK")


def test_bindings_culled():
    # the redesign cut m/d/g/v/O/T; core keys remain. Locks the simplified surface.
    keys = {b.key for b in tui.CCApp.BINDINGS}
    for gone in ("m", "d", "g", "v", "O", "T"):
        assert gone not in keys, "binding %s should be removed" % gone
    for kept in ("o", "n", "P", "G", "L", "M", "x", "q", "a"):
        assert kept in keys, "binding %s should remain" % kept


if __name__ == "__main__":
    test_bindings_culled()
    print("ok test_bindings_culled")
    asyncio.run(_smoke())
    print("ok test_tui_smoke")
    test_parse_mr_plan()
    print("ok test_parse_mr_plan")
    asyncio.run(_mrconfirm())
    print("ok test_mrconfirm")
    test_group_options()
    print("ok test_group_options")
    asyncio.run(_movescreen())
    print("ok test_movescreen")
    test_group_panel_rows()
    print("ok test_group_panel_rows")
    asyncio.run(_panelscreen())
    print("ok test_panelscreen")
    asyncio.run(_confirmscreen())
    print("ok test_confirmscreen")
