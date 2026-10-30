"""Translation and headless language-picker tests, isolated from real user state."""

import ast
import asyncio
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cc
import tui
import tui_i18n as i18n
from textual.widgets import Button, Input, Select, Static, Tree


class CatalogTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ru")
        i18n._catalogs.clear()

    def test_catalogs_cover_templates_and_preserve_formatting(self):
        catalogs = {code: json.loads((i18n.CATALOG_DIR / f"{code}.json").read_text())
                    for code in i18n.LANGUAGES}
        tree = ast.parse((ROOT / "tui.py").read_text())
        messages = {tui.LEGEND, *tui._AUDIT_VERB.values(), *tui._AUDIT_FIELD.values()}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "tr" and isinstance(node.args[0], ast.Constant):
                    messages.add(node.args[0].value)
                elif node.func.id == "Binding":
                    messages.add(node.args[2].value)
        placeholders = re.compile(r"%(?:\([^)]+\))?[-+#0 ]*\d*(?:\.\d+)?[sdif]")
        markup = re.compile(r"\[/?(?:b|dim|red|green|yellow|bold red|bold green|bold yellow)\]")
        for code, catalog in catalogs.items():
            self.assertFalse(messages - catalog.keys(), (code, messages - catalog.keys()))
            self.assertEqual(catalog.keys(), catalogs["en"].keys())
            for source, translated in catalog.items():
                self.assertIsInstance(translated, str)
                self.assertTrue(translated, (code, source))
                self.assertEqual(placeholders.findall(source), placeholders.findall(translated), (code, source))
                self.assertEqual(markup.findall(source), markup.findall(translated), (code, source))
                if code == "en":
                    self.assertEqual(source, translated)

    def test_language_lookup_and_fallback(self):
        i18n.set_language("ru")
        self.assertEqual(i18n.tr("Cancel"), "Отмена")
        self.assertEqual(i18n.tr("Cancel", language="en"), "Cancel")
        self.assertEqual(i18n.get_language(), "ru")
        self.assertEqual(i18n.tr("Missing %s") % "Русский project", "Missing Русский project")
        i18n.set_language("en")
        self.assertEqual(i18n.tr("Cancel"), "Cancel")
        i18n.set_language("unsupported")
        self.assertEqual(i18n.get_language(), "ru")
        with patch.dict(i18n._catalogs, {"ru": {"Cancel": None, "Apply": ""}}):
            self.assertEqual(i18n.tr("Cancel"), "Cancel")
            self.assertEqual(i18n.tr("Apply"), "Apply")

    def test_timeline_and_recovery_preserve_user_content(self):
        records = [{"ts": 1, "action": "task.add", "task": "t1", "title": "Сохранить [текст]",
                    "repos": ["web", "api"], "branch": "feature"},
                   {"ts": 2, "action": "future.action", "detail": "Original log"}]
        for code, field in [("en", "title"), ("ru", "название")]:
            i18n.set_language(code)
            lines = "\n".join(tui._timeline_lines(json.dumps(records)))
            self.assertIn(i18n.tr("created"), lines)
            self.assertIn(field + "=Сохранить [текст]", lines)
            self.assertIn("web,api", lines)
            self.assertIn("future.action", lines)
            self.assertIn("Original log", lines)
            self.assertEqual(tui._timeline_lines("[]"), [i18n.tr("(timeline is empty — no actions recorded yet)")])
            self.assertEqual(tui._recovery_lines("[]"), [i18n.tr("No lost tasks on disk to recover ✓")])
            recovered = tui._recovery_lines(json.dumps([["t1", "E1", "feature"]]))
            self.assertEqual(recovered[0], i18n.tr("Recovered %d tasks to the board:") % 1)
            self.assertIn("feature", recovered[1])

    def test_missing_or_malformed_catalog(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(i18n, "CATALOG_DIR", Path(directory)):
            for contents in (None, "not json", "[]"):
                if contents is not None:
                    (Path(directory) / "ru.json").write_text(contents)
                i18n._catalogs.clear()
                self.assertEqual(i18n.tr("Cancel"), "Cancel")

    def test_preferences_validation_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            preferences = Path(directory) / "nested" / "tui.json"
            self.assertEqual(i18n.load_language(preferences), "ru")
            i18n.save_language("en", preferences)
            self.assertEqual(i18n.load_language(preferences), "en")
            before = preferences.read_bytes()
            with patch.object(i18n.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    i18n.save_language("ru", preferences)
            self.assertEqual(preferences.read_bytes(), before)
            self.assertEqual(list(preferences.parent.glob(".tui-*.tmp")), [])
            with self.assertRaises(ValueError):
                i18n.save_language("unsupported", preferences)
            self.assertEqual(preferences.read_bytes(), before)
            for value in ("invalid JSON", "[]", "null", '{"language": []}', '{"language": "zz"}'):
                preferences.write_text(value)
                self.assertEqual(i18n.load_language(preferences), "ru")


class LanguagePickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.preferences = root / "tui.json"
        self.state_file = root / "state.json"
        self.state = {
            "projects": {"Русский project": {"path": str(root), "repos": {"web": {}}}},
            "epics": {"E1": {"project": "Русский project", "summary": "Фича", "mode": "epic_branch"}},
            "tasks": {"t1": {"epic": "E1", "title": "Сохранить текст", "branch": "feature",
                              "status": "review", "repos": ["web"], "base": {"web": "E1"}, "mrs": {}}},
        }
        self.state_file.write_text(json.dumps(self.state))
        for target, name, value in [
            (cc, "STATE_FILE", self.state_file), (cc, "AUDIT_FILE", root / "audit.log"),
            (cc, "_scan_orphans", lambda state: []),
            (tui.CCApp, "_bg", lambda *args: None),
            (tui.CCApp, "_daemon", lambda *args: None),
        ]:
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(i18n.set_language, "ru")

    async def choose(self, app, pilot, code):
        """`choose(app, pilot, code)` applies CODE through the visible language-picker controls."""
        await pilot.press("f2")
        self.assertIsInstance(app.screen, tui.LanguageScreen)
        app.screen.query_one("#language-choice", Select).value = code
        await pilot.pause()
        self.assertEqual(str(app.screen.query_one("#apply-language", Button).label),
                         "Apply" if code == "en" else "Применить")
        self.assertEqual(str(app.screen.query_one("#cancel-language", Button).label),
                         "Cancel" if code == "en" else "Отмена")
        await pilot.click("#apply-language")
        await pilot.pause()

    async def test_live_switch_persistence_selection_and_dialogs(self):
        app = tui.CCApp(language_file=self.preferences)
        async with app.run_test(size=(120, 40)) as pilot:
            tree = app.query_one(Tree)
            project = tree.root.children[0]
            project.expand()
            project.children[0].expand()
            await pilot.pause()
            tree.move_cursor(project.children[0].children[0])
            await pilot.pause()
            self.assertIn("ждёт тебя", str(app.query_one("#legend", Static).render()))
            self.assertEqual(app.current()["id"], "t1")
            for code, quit_label, cancel_label in [("en", "Quit", "Cancel"), ("ru", "Выход", "Отмена"), ("en", "Quit", "Cancel")]:
                await self.choose(app, pilot, code)
                self.assertEqual(i18n.get_language(), code)
                self.assertEqual(app.current()["id"], "t1")
                self.assertTrue(tree.root.children[0].is_expanded)
                detail = str(app.query_one("#detail", Static).render())
                self.assertIn("Сохранить текст", detail)
                self.assertIn("Needs review" if code == "en" else "Ждёт ревью", detail)
                self.assertEqual(app.screen.active_bindings["q"].binding.description, quit_label)
                app.push_screen(tui.AddProjectScreen())
                await pilot.pause()
                self.assertEqual(str(app.screen.query_one("#cancel", Button).label), cancel_label)
                self.assertEqual(app.screen.query_one("#ppath", Input).placeholder,
                                 "project folder path" if code == "en" else "путь к папке проекта")
                await pilot.click("#cancel")
                await pilot.pause()
            app.push_screen(tui.ConfirmScreen("Title", "User content"))
            await pilot.pause()
            self.assertEqual(str(app.screen.query_one("#ok", Button).label), "Yes")
            self.assertEqual(app.screen.active_bindings["escape"].binding.description, "Cancel")
            await pilot.press("escape")
        self.assertEqual(json.loads(self.state_file.read_text()), self.state)
        restarted = tui.CCApp(language_file=self.preferences)
        async with restarted.run_test() as pilot:
            self.assertEqual(i18n.get_language(), "en")
            self.assertIn("needs review", str(restarted.query_one("#legend", Static).render()))
        self.assertEqual(tui.CCApp.BINDINGS[-1].description, "Quit")

    async def test_cancel_and_save_failure_leave_language_unchanged(self):
        app = tui.CCApp(language_file=self.preferences)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("f2")
            for code, apply_label, cancel_label in [("ru", "Применить", "Отмена"), ("en", "Apply", "Cancel"),
                                                    ("ru", "Применить", "Отмена"), ("en", "Apply", "Cancel")]:
                app.screen.query_one(Select).value = code
                await pilot.pause()
                self.assertEqual(str(app.screen.query_one("#apply-language", Button).label), apply_label)
                self.assertEqual(str(app.screen.query_one("#cancel-language", Button).label), cancel_label)
                self.assertEqual(i18n.get_language(), "ru")
                self.assertFalse(self.preferences.exists())
            await pilot.click("#cancel-language")
            self.assertEqual(i18n.get_language(), "ru")
            self.assertFalse(self.preferences.exists())
            with patch.object(i18n, "save_language", side_effect=OSError("read-only")), patch.object(app, "notify") as notify:
                await self.choose(app, pilot, "en")
                self.assertEqual(i18n.get_language(), "ru")
                self.assertEqual(notify.call_args.kwargs["severity"], "error")
                self.assertIn("read-only", notify.call_args.args[0])
            self.assertFalse(self.preferences.exists())

    async def test_english_node_details_and_confirmation(self):
        app = tui.CCApp(language="en", language_file=self.preferences)
        async with app.run_test(size=(100, 32)) as pilot:
            for kind, key in [("project", "Русский project"), ("epic", "E1"), ("task", "t1")]:
                app.show_detail({"type": kind, "id": key})
                self.assertIn(key if kind != "task" else "Сохранить текст",
                              str(app.query_one("#detail", Static).render()))
            with patch.object(tui.MRConfirmScreen, "_compute"):
                screen = tui.MRConfirmScreen("Create MR", [])
                app.push_screen(screen)
                await pilot.pause()
                screen._show_plan([("web", "feature", "main", True)])
                text = str(screen.query_one("#mrc", Static).render())
                self.assertIn("production release", text)
                self.assertIn("feature", text)
                self.assertIn("main", text)
                await pilot.press("escape")

    async def test_lost_tasks_details_switch_language_and_keep_selection(self):
        orphan = {"project": "Русский project", "epic": "LOST", "slug": "ghost", "branch": "LOST-ghost",
                  "repos": {"web": "/tmp/[worktree]/web"}}
        app = tui.CCApp(language_file=self.preferences)
        with patch.object(cc, "_scan_orphans", return_value=[orphan]):
            async with app.run_test(size=(120, 40)) as pilot:
                tree = app.query_one(Tree)
                parent = tree.root.children[-1]
                tree.move_cursor(parent)
                await pilot.pause()
                self.assertIn("Потерянные задачи", str(app.query_one("#detail", Static).render()))
                tree.move_cursor(parent.children[0])
                await pilot.pause()
                for code, label in [("en", "Lost tasks"), ("ru", "Потерянные задачи")]:
                    await self.choose(app, pilot, code)
                    detail = str(app.query_one("#detail", Static).render())
                    self.assertIn(label, detail)
                    self.assertIn("/tmp/[worktree]/web", detail)
                    self.assertEqual(app.current()["orphan"], orphan)

    async def test_timeline_filters_and_project_errors_are_visible(self):
        app = tui.CCApp(language="en", language_file=self.preferences)
        async with app.run_test(size=(120, 40)) as pilot:
            for current, flag in [(None, None), ({"type": "task", "id": "t1"}, "--task"),
                                  ({"type": "epic", "id": "E1"}, "--epic")]:
                with patch.object(app, "current", return_value=current):
                    app.action_timeline()
                await pilot.pause()
                screen = app.screen
                self.assertIn("--json", screen._argv)
                if flag:
                    self.assertEqual(screen._argv[-2:], [flag, current["id"]])
                proc = Mock(stdout=io.StringIO('[]\n'), returncode=0)
                with patch.object(tui.subprocess, "Popen", return_value=proc):
                    await asyncio.to_thread(screen._run)
                self.assertIn("timeline is empty", str(screen.query_one("#ocontent", Static).render()))
                await pilot.press("escape")
            app._project_added({"path": "/tmp/project with spaces", "name": "demo"})
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen._argv, [sys.executable, tui.ENGINE, "project", "add",
                                           "/tmp/project with spaces", "demo"])
            error = "Traceback:\n" + "stack frame\n" * 20 + "FileNotFoundError: missing executable\n"
            with patch.object(tui.subprocess, "Popen", return_value=Mock(stdout=io.StringIO(error), returncode=1)):
                await asyncio.to_thread(screen._run)
            output = str(screen.query_one("#ocontent", Static).render())
            self.assertIn("FileNotFoundError: missing executable", output)
            self.assertIn("ERROR (exit 1)", output)
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
