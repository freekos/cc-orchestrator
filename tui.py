#!/usr/bin/env python3
"""cc TUI — terminal interface over the cc engine (Phase 3, Textual)."""
import json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cc

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Tree, Static, Input, Button, Label, TextArea, Select
from textual.binding import Binding
import threading

ENGINE = str(Path(__file__).parent / "cc.py")
GLYPH = {"running": "🔵", "review": "🟡", "mr": "🟣", "merged": "✅", "idle": "⚪", "done": "✅"}


def fast_status(t):
    """Cheap status for rendering: live agent -> running; otherwise the cached status computed
    off-thread by _refresh_statuses (it encodes merged-with-new-edits -> review). Falls back to
    flags only before the first refresh."""
    pid = t.get("pid")
    if pid and cc.pid_alive(pid):
        return "running"
    st = t.get("status")
    if st in ("review", "mr", "merged", "idle"):
        return st
    if t.get("merged"):
        return "merged"
    if t.get("mrs"):
        return "mr"
    return "review"


class NewEpicScreen(ModalScreen):
    CSS = """
    NewEpicScreen { align: center middle; }
    #dlg { width: 88; max-width: 94%; height: auto; max-height: 92%;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg > Label { margin-bottom: 1; }
    #body { height: auto; max-height: 26; overflow-y: auto; margin-bottom: 1; }
    #body Label { margin-bottom: 0; }
    #body Input { margin-bottom: 1; width: 1fr; }
    #body Select { margin-bottom: 1; width: 1fr; }
    #body Button { margin: 0 0 1 0; min-width: 18; }
    .sep { margin: 1 0 0 0; text-align: center; color: $text-disabled; }
    #row { height: auto; align-horizontal: right; }
    #row Button { margin: 0 0 0 2; min-width: 14; }
    """

    def __init__(self, project, jira_on=False):
        super().__init__()
        self.project = project
        self.jira_on = jira_on
        self._epics = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            if self.jira_on:
                yield Label("[b]Epic из Jira[/b] для '%s'" % self.project)
                with VerticalScroll(id="body"):
                    yield Label("[dim]Взять существующий эпик (свежие сверху; поиск — по названию):[/dim]")
                    yield Input(placeholder="поиск по названию — Enter (по всем эпикам проекта)", id="esearch")
                    yield Select([("(загрузка эпиков проекта…)", "_")], id="epick", allow_blank=True)
                    yield Button("Взять выбранный", variant="success", id="use")
                    yield Static("──────────  или  ──────────", classes="sep")
                    yield Label("[dim]Создать новый эпик в Jira:[/dim]")
                    yield Input(placeholder="название нового эпика", id="enew")
                    yield Button("Создать в Jira", variant="primary", id="createnew")
                with Horizontal(id="row"):
                    yield Button("Cancel", id="cancel")
            else:
                yield Label("[b]Новый эпик[/b] в проекте '%s'" % self.project)
                with VerticalScroll(id="body"):
                    yield Input(placeholder="key (напр. IK-8631)", id="key")
                    yield Input(placeholder="summary", id="summary")
                    yield Input(placeholder="targets repo=branch,… (пусто = эпик получит свою ветку)", id="targets")
                    yield Input(placeholder="repos через запятую; пусто = ВСЕ", id="erepos")
                with Horizontal(id="row"):
                    yield Button("Cancel", id="cancel")
                    yield Button("Create", variant="success", id="ok")

    def on_mount(self):
        if self.jira_on:
            self.app._daemon(lambda: self._load_epics(""))

    def _load_epics(self, query):
        s = cc.load_state()
        cfg = s["projects"][self.project].get("jira", {})
        try:
            self._epics = cc.jira_my_epics(cfg, query)
        except Exception:
            self._epics = []
        self.app.call_from_thread(self._fill)

    def _fill(self):
        try:
            sel = self.query_one("#epick", Select)
            opts = [("%s — %s [%s]" % (e["key"], e["summary"][:42], e.get("status", "")), e["key"])
                    for e in self._epics]
            sel.set_options(opts or [("(нет — впиши поиск или создай новый ниже)", "_")])
        except Exception:
            pass

    def on_input_submitted(self, event):
        if self.jira_on and event.input.id == "esearch":
            self.app._daemon(lambda: self._load_epics(event.value.strip()))

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "ok":
            key = self.query_one("#key", Input).value.strip()
            if not key:
                self.app.notify("key is required", severity="error"); return
            self.dismiss({"key": key,
                          "summary": self.query_one("#summary", Input).value.strip(),
                          "targets": self.query_one("#targets", Input).value.strip(),
                          "repos": self.query_one("#erepos", Input).value.strip()})
            return
        if event.button.id == "use":
            val = self.query_one("#epick", Select).value
            if val in (None, "_", Select.BLANK):
                self.app.notify("выбери эпик из списка", severity="error"); return
            summary = next((e["summary"] for e in self._epics if e["key"] == val), "")
            self.dismiss({"action": "pick", "key": str(val), "summary": summary}); return
        if event.button.id == "createnew":
            summary = self.query_one("#enew", Input).value.strip()
            if not summary:
                self.app.notify("введи название эпика", severity="error"); return
            self.dismiss({"action": "create", "summary": summary}); return


class NewTaskScreen(ModalScreen):
    CSS = """
    NewTaskScreen { align: center middle; }
    #dlg { width: 92; max-width: 94%; height: auto; max-height: 92%;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg Label { margin-bottom: 1; }
    #body { height: auto; max-height: 28; overflow-y: auto; margin-bottom: 1; }
    #body Input { margin-bottom: 1; width: 1fr; }
    #body Select { margin-bottom: 1; width: 1fr; }
    #body TextArea { height: 7; margin-bottom: 0; width: 1fr; }
    #jira { height: auto; border: round $primary 50%; padding: 0 1; margin-bottom: 1; }
    #jira Label { margin-bottom: 0; }
    #jira Button { margin: 0; width: 1fr; }
    #row { height: auto; align-horizontal: right; }
    #row Button { margin: 0 0 0 2; min-width: 14; }
    """

    def __init__(self, epic, jira_on=False, project=None, preset_key=None, preset_summary=""):
        super().__init__()
        self.epic = epic
        self.jira_on = jira_on
        self.project = project
        self.preset_key = preset_key
        self.preset_summary = preset_summary
        self._children = []
        self._jira_key = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("[b]Новая задача[/b] под эпиком '%s'" % self.epic)
            yield Label("[dim]идёт по всем репо проекта; MR — только где агент менял[/dim]")
            with VerticalScroll(id="body"):
                if self.jira_on:
                    with Vertical(id="jira"):
                        yield Label("[dim]из Jira (опц.): задача эпика ИЛИ без эпика (перенесём под эпик) → привяжем + засидим[/dim]")
                        yield Input(placeholder="поиск задач эпика (Enter)", id="tsearch")
                        yield Select([("(загрузка задач эпика…)", "_")], id="tpick", allow_blank=True)
                        yield Button("Подставить из Jira", id="seed")
                yield Label("title:")
                yield Input(placeholder="напр. fix loyalty badge", id="title")
                yield Label("prompt (что сделать агенту):")
                yield TextArea(id="prompt")
            with Horizontal(id="row"):
                yield Button("Cancel", id="cancel")
                yield Button("Launch", variant="success", id="ok")

    def on_mount(self):
        if self.jira_on:
            self.app._daemon(lambda: self._load_children(""))
        if self.preset_key:
            self._jira_key = self.preset_key
            self.app._daemon(self._seed_preset)

    def _seed_preset(self):
        key = self.preset_key
        summary = self.preset_summary or key
        cfg = cc.load_state()["projects"][self.project].get("jira", {})
        try:
            desc = cc.jira_epic_description(cfg, key)
        except Exception:
            desc = ""
        def fill():
            self.query_one("#title", Input).value = summary
            seed = ("%s\n\n%s" % (summary, desc)).strip() if desc else summary
            self.query_one("#prompt", TextArea).load_text(seed)
            self.app.notify("из %s — отредактируй prompt и Launch" % key)
        self.app.call_from_thread(fill)

    def _load_children(self, query):
        try:
            cfg = cc.load_state()["projects"][self.project].get("jira", {})
            children = cc.jira_epic_children(cfg, self.epic, query)
            seen = {c["key"] for c in children}
            orphans = [o for o in cc.jira_orphan_tasks(cfg, query) if o["key"] not in seen]
            self._children = children + orphans   # epic tasks first, then parentless ones
        except Exception:
            self._children = []
        self.app.call_from_thread(self._fill)

    def _fill(self):
        try:
            sel = self.query_one("#tpick", Select)
            opts = []
            for c in self._children:
                tag = "  · БЕЗ ЭПИКА (перенесём)" if c.get("orphan") else ""
                opts.append(("%s — %s [%s]%s" % (c["key"], c["summary"][:38], c["status"], tag), c["key"]))
            sel.set_options(opts or [("(задач не найдено)", "_")])
        except Exception:
            pass

    def on_input_submitted(self, event):
        if self.jira_on and event.input.id == "tsearch":
            self.app._daemon(lambda: self._load_children(event.value.strip()))

    def _seed(self, key):
        cfg = cc.load_state()["projects"][self.project].get("jira", {})
        summary = next((c["summary"] for c in self._children if c["key"] == key), key)
        try:
            desc = cc.jira_epic_description(cfg, key)
        except Exception:
            desc = ""
        def fill():
            self.query_one("#title", Input).value = summary
            seed = ("%s\n\n%s" % (summary, desc)).strip() if desc else summary
            self.query_one("#prompt", TextArea).load_text(seed)
            self.app.notify("подставлено из %s — отредактируй prompt и Launch" % key)
        self.app.call_from_thread(fill)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "seed":
            val = self.query_one("#tpick", Select).value
            if val in (None, "_", Select.BLANK):
                self.app.notify("выбери задачу из списка", severity="error"); return
            self._jira_key = str(val)
            self.app._daemon(lambda: self._seed(str(val))); return
        title = self.query_one("#title", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not title or not prompt:
            self.app.notify("title and prompt are required", severity="error"); return
        self.dismiss({"epic": self.epic, "title": title, "prompt": prompt, "jira": self._jira_key})


CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"

def _cmux_path():
    return shutil.which("cmux") or (CMUX_BIN if os.path.exists(CMUX_BIN) else None)

def open_in_terminal(cwd, cmd):
    """Fallback: open cmd in a new Terminal.app/iTerm tab."""
    full = "cd %s && %s" % (shlex.quote(cwd), cmd)
    esc = full.replace("\\", "\\\\").replace('"', '\\"')
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        subprocess.Popen(["osascript", "-e", 'tell application "iTerm"',
                          "-e", 'tell current window to create tab with default profile',
                          "-e", 'tell current session of current window to write text "%s"' % esc,
                          "-e", 'end tell'])
    else:
        subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to do script "%s"' % esc,
                          "-e", 'tell application "Terminal" to activate'])



class DiffScreen(ModalScreen):
    CSS = """
    DiffScreen { align: center middle; }
    #diffbox { width: 92%; height: 90%; border: thick $accent; background: $surface; }
    #diffcontent { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, tid):
        super().__init__()
        self.tid = tid

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="diffbox"):
            yield Static(id="diffcontent")
        yield Footer()

    def on_mount(self):
        from rich.text import Text
        s = cc.load_state(); t = s["tasks"].get(self.tid)
        txt = Text()
        if not t:
            self.query_one("#diffcontent", Static).update("task gone"); return
        self.title = "diff: %s" % t["title"]
        any_change = False
        for r in t["repos"]:
            wt = t["worktrees"][r]
            if not os.path.isdir(wt):
                continue
            cc.git(["add", "-A", "--intent-to-add", "."], cwd=wt, check=False)
            diff = cc.git(["diff"], cwd=wt, check=False).stdout
            txt.append("\n\u2550\u2550 %s \u2550\u2550\n" % r, style="bold yellow")
            if not diff.strip():
                txt.append("(no changes)\n", style="dim"); continue
            any_change = True
            for line in diff.splitlines():
                if line.startswith("+++") or line.startswith("---") or line.startswith("diff "):
                    txt.append(line + "\n", style="bold white")
                elif line.startswith("+"):
                    txt.append(line + "\n", style="green")
                elif line.startswith("-"):
                    txt.append(line + "\n", style="red")
                elif line.startswith("@@"):
                    txt.append(line + "\n", style="cyan")
                else:
                    txt.append(line + "\n")
        if not any_change:
            txt.append("\n(no changes across repos yet)\n", style="dim")
        self.query_one("#diffcontent", Static).update(txt)

    def action_close(self):
        self.dismiss(None)


class ChatScreen(ModalScreen):
    CSS = """
    ChatScreen { align: center middle; }
    #chatbox { width: 92%; height: 90%; border: thick $accent; background: $surface; }
    #chatcontent { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"),
                Binding("i", "reply", "Reply (new tab)"), Binding("o", "open_links", "Открыть ссылки")]

    def action_open_links(self):
        urls = []
        if self._file and self._file.exists():
            urls = re.findall(r"https?://[^\s)\]\"']+", self._file.read_text(errors="replace"))
        urls = list(dict.fromkeys(urls))
        if not urls:
            self.app.notify("ссылок в переписке нет"); return
        for u in urls:
            subprocess.Popen(["open", u])
        self.app.notify("открыл %d ссылок" % len(urls))

    def __init__(self, tid):
        super().__init__()
        self.tid = tid
        self._t = None
        self._file = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chatbox"):
            yield Static(id="chatcontent")
        yield Footer()

    def on_mount(self):
        try:
            box = self.query_one("#chatbox", VerticalScroll)
            box.border_title = "CHAT (read-only)  ·  i = ответить агенту  ·  esc = закрыть"
        except Exception:
            pass
        self._resolve()
        self.reload()
        self.set_interval(2.0, self.reload)

    def _resolve(self):
        s = cc.load_state()
        self._t = s["tasks"].get(self.tid)
        if not self._t:
            self._file = None
            return
        pw = self._t.get("dir") or self._t["worktrees"][self._t["primary"]]
        sid = self._t.get("claude_session", {}).get(self._t["primary"]) or cc.resolve_session(pw)
        self._file = (cc.CLAUDE_PROJECTS / pw.replace("/", "-") / (sid + ".jsonl")) if sid else None

    def reload(self):
        from rich.text import Text
        txt = Text()
        if not self._t:
            self.query_one("#chatcontent", Static).update("task gone")
            return
        txt.append(" 👁  ПРОСМОТР (read-only) — печатать здесь нельзя.  ", style="bold black on yellow")
        txt.append("нажми ", style="dim")
        txt.append("i", style="bold cyan")
        txt.append(" чтобы ответить агенту (новая вкладка)\n", style="dim")
        txt.append("─" * 60 + "\n", style="dim")
        if not self._file or not self._file.exists():
            txt.append("агент ещё не начал или транскрипт пуст.\n\n", style="dim")
            txt.append("нажми  i  — впрыгнуть в чат и ответить (новая вкладка)", style="cyan")
            self.query_one("#chatcontent", Static).update(txt)
            return
        msgs = []
        for ln in self._file.read_text(errors="replace").splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            ty = o.get("type")
            if ty == "user":
                c = o.get("message", {}).get("content")
                if isinstance(c, str):
                    body = c.split("\n\n[cc]")[0].strip()
                    if body:
                        msgs.append(("you", body))
            elif ty == "assistant":
                for b in (o.get("message", {}).get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        msgs.append(("agent", b["text"].strip()))
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        hint = ""
                        for k in ("file_path", "command", "path", "pattern", "description", "prompt"):
                            if k in inp:
                                hint = str(inp[k]); break
                        msgs.append(("tool", "%s %s" % (b.get("name", ""), hint[:70])))
        for who, body in msgs[-40:]:
            if who == "you":
                txt.append("\n► ты: ", style="bold cyan"); txt.append(body[:1500] + "\n")
            elif who == "agent":
                txt.append("\n● агент: ", style="bold green"); txt.append(body[:2500] + "\n")
            else:
                txt.append("   ⚙ %s\n" % body, style="yellow")
        if not msgs:
            txt.append("(пусто)", style="dim")
        self.query_one("#chatcontent", Static).update(txt)
        try:
            self.query_one("#chatbox", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def action_close(self):
        self.dismiss(None)

    def action_reply(self):
        t = self._t
        if not t:
            return
        pw = t.get("dir") or t["worktrees"][t["primary"]]
        if not os.path.isdir(pw):
            self.app.notify("worktree missing", severity="error"); return
        sid = t.get("claude_session", {}).get(t["primary"]) or cc.resolve_session(pw)
        chat = ("claude --resume %s --permission-mode auto" % sid) if sid else "claude --permission-mode auto"
        self.app._open_chat_tab("cc:%s" % self.tid, pw, chat)
        self.dismiss(None)
        self.app.notify("чат %s открыт в новой вкладке" % self.tid)


class AddProjectScreen(ModalScreen):
    CSS = """
    AddProjectScreen { align: center middle; }
    #dlg { width: 86; max-width: 92%; height: auto; max-height: 90%; overflow-y: auto;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg Label { margin-bottom: 1; }
    #dlg Input { margin-bottom: 1; width: 1fr; }
    #dlg Select { margin-bottom: 1; width: 1fr; }
    #dlg Button { margin: 0 2 1 0; }
    #row { height: auto; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("Add project — папка с репо (single или multi-repo)")
            yield Input(placeholder="путь к папке проекта", id="ppath")
            yield Input(placeholder="имя (опц.; по умолчанию = имя папки)", id="pname")
            with Horizontal(id="row"):
                yield Button("Browse…", id="browse")
                yield Button("Add", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "browse":
            r = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Выбери папку проекта")'],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                self.query_one("#ppath", Input).value = r.stdout.strip().rstrip("/")
            return
        path = self.query_one("#ppath", Input).value.strip()
        if not path:
            self.app.notify("укажи путь к папке", severity="error"); return
        self.dismiss({"path": path, "name": self.query_one("#pname", Input).value.strip()})


class OutputScreen(ModalScreen):
    CSS = """
    OutputScreen { align: center middle; }
    #obox { width: 90%; height: 85%; border: thick $accent; background: $surface; }
    #ocontent { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"),
                Binding("o", "open_links", "Открыть ссылки")]

    def __init__(self, title, argv):
        super().__init__()
        self._title = title
        self._argv = argv
        self._lines = []

    def action_open_links(self):
        urls = []
        for ln in self._lines:
            urls += re.findall(r"https?://[^\s)\]\"']+", ln)
        urls = list(dict.fromkeys(urls))
        if not urls:
            self.app.notify("ссылок в выводе нет"); return
        for u in urls:
            subprocess.Popen(["open", u])
        self.app.notify("открыл %d ссылок в браузере" % len(urls))

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="obox"):
            yield Static("⏳ %s …" % self._title, id="ocontent")
        yield Footer()

    def on_mount(self):
        self._lines = ["⏳ %s — выполняю…" % self._title]
        try:
            self.query_one("#obox", VerticalScroll).border_title = "%s  ·  esc = закрыть" % self._title
        except Exception:
            pass
        self.app._bg("output:%d" % id(self), self._run)

    def _run(self):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"   # never block on a credential prompt
        try:
            proc = subprocess.Popen(self._argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=env)
            for line in proc.stdout:
                self.app.call_from_thread(self._append, line.rstrip("\n"))
            proc.wait()
            tail = "✓ готово (exit 0)" if proc.returncode == 0 else "✗ ОШИБКА (exit %d)" % proc.returncode
        except Exception as ex:
            tail = "✗ ОШИБКА запуска: %s" % ex
        self.app.call_from_thread(self._append, "")
        self.app.call_from_thread(self._append, tail)

    def _append(self, line):
        if self._lines == ["⏳ %s — выполняю…" % self._title]:
            self._lines = []   # drop the placeholder on first real line
        self._lines.append(line)
        from rich.text import Text
        txt = Text()
        for ln in self._lines[-300:]:
            if "http" in ln:
                txt.append(ln + "\n", style="cyan")
            elif ln.startswith("✓") or " MR -> " in ln:
                txt.append(ln + "\n", style="bold green")
            elif ln.startswith("✗") or "FAIL" in ln.upper():
                txt.append(ln + "\n", style="bold red")
            elif "DRY-RUN" in ln or "skipped" in ln:
                txt.append(ln + "\n", style="yellow")
            elif ln.strip().startswith("would:") or ln.startswith("⏳"):
                txt.append(ln + "\n", style="dim")
            else:
                txt.append(ln + "\n")
        self.query_one("#ocontent", Static).update(txt)
        try:
            self.query_one("#obox", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def action_close(self):
        self.dismiss(None)


class ReviewersScreen(ModalScreen):
    CSS = """
    ReviewersScreen { align: center middle; }
    #rbox { width: 84; height: 85%; border: thick $accent; background: $surface; padding: 1 2; }
    .rrow { height: 3; }
    .rrow Label { width: 26; content-align: left middle; }
    .rrow Select { width: 1fr; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, project):
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="rbox"):
            yield Static("[b]Reviewer на репо[/b] — выбери из участников GitLab. esc = закрыть\n")
            s = cc.load_state()
            for r in s["projects"][self.project]["repos"]:
                with Horizontal(classes="rrow"):
                    yield Label(r)
                    yield Select([("(загрузка…)", "_")], id="sel__" + r, allow_blank=True)
        yield Footer()

    def on_mount(self):
        try:
            self.query_one("#rbox", VerticalScroll).border_title = "Reviewers — %s" % self.project
        except Exception:
            pass
        self.app._daemon(self._load)

    def _load(self):
        s = cc.load_state()
        for r, ri in s["projects"][self.project]["repos"].items():
            members = cc.glab_members(ri["remote"])
            opts = [("%s — %s" % (m["username"], m["name"]), m["username"]) for m in members]
            self.app.call_from_thread(self._set_opts, r, opts, ri.get("reviewer", ""))

    def _set_opts(self, repo, opts, cur):
        try:
            sel = self.query_one("#sel__" + repo, Select)
            sel.set_options(opts or [("(нет участников)", "_")])
            if cur and cur in [v for _, v in opts]:
                sel.value = cur
        except Exception:
            pass

    def on_select_changed(self, event):
        sid = event.select.id or ""
        if not sid.startswith("sel__"):
            return
        val = event.value
        if val in (None, "_", Select.BLANK):
            return
        repo = sid[len("sel__"):]
        s = cc.load_state()
        cur = s["projects"][self.project]["repos"].get(repo, {}).get("reviewer", "")
        if str(val) == cur:
            return
        subprocess.run([sys.executable, ENGINE, "repo", "set", self.project, repo, "--reviewer", str(val)],
                       capture_output=True)
        self.app.notify("%s -> reviewer %s" % (repo, val))

    def action_close(self):
        self.dismiss(None)


class ProjectJiraScreen(ModalScreen):
    CSS = """
    ProjectJiraScreen { align: center middle; }
    #dlg { width: 84; max-width: 92%; height: auto; max-height: 90%; overflow-y: auto;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg Label { margin-bottom: 1; }
    #dlg Input { margin-bottom: 1; width: 1fr; }
    #dlg Select { margin-bottom: 1; width: 1fr; }
    #dlg Button { margin: 0 2 1 0; }
    #row { height: auto; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, project):
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        s = cc.load_state()
        j = s["projects"][self.project].get("jira", {})
        with Vertical(id="dlg"):
            yield Label("Jira для проекта '%s'" % self.project)
            yield Label("[dim]нет токена? кликни ссылку (или кнопку) — откроется в браузере:[/dim]")
            yield Static("[@click=screen.open_token][u]https://id.atlassian.com/manage-profile/security/api-tokens[/u][/]")
            yield Input(value=j.get("site", ""), placeholder="site (you.atlassian.net)", id="jsite")
            yield Input(value=j.get("email", ""), placeholder="email", id="jemail")
            yield Input(placeholder=("token: ••• (введи чтобы изменить)" if j.get("token") else "API-token (вставь сюда)"),
                        id="jtoken", password=True)
            yield Input(value=j.get("project_key", ""), placeholder="project key (e.g. IK)", id="jkey")
            with Horizontal(id="row"):
                yield Button("Сохранить", variant="success", id="save")
                yield Button("Отвязать Jira", id="off")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "off":
            subprocess.run([sys.executable, ENGINE, "project", "jira", self.project, "--off"], capture_output=True)
            self.app.notify("Jira выключена для %s" % self.project); self.dismiss(True); return
        args = [sys.executable, ENGINE, "project", "jira", self.project]
        site = self.query_one("#jsite", Input).value.strip()
        email = self.query_one("#jemail", Input).value.strip()
        token = self.query_one("#jtoken", Input).value.strip()
        key = self.query_one("#jkey", Input).value.strip()
        if site:
            args += ["--site", site]
        if email:
            args += ["--email", email]
        if token:
            args += ["--token", token]
        if key:
            args += ["--project-key", key]
        r = subprocess.run(args, capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        if "ok —" in out:
            self.app.notify("Jira подключена ✓ (%s)" % (out.split("ok —")[1].strip()[:60]))
        elif "WARN" in out:
            self.app.notify("сохранено, но auth не прошла — проверь токен/email", severity="error")
        else:
            self.app.notify("Jira сохранена для %s" % self.project)
        self.dismiss(True)

    def action_open_token(self):
        subprocess.Popen(["open", "https://id.atlassian.com/manage-profile/security/api-tokens"])
        self.app.notify("открыл страницу создания токена в браузере")

    def action_close(self):
        self.dismiss(None)


class EpicManageScreen(ModalScreen):
    """Готово и убрать с доски: Jira(эпик + все задачи) -> Done, затем убрать эпик и его задачи
    из cc локально (worktrees/ветки/папки). Remote MR/ветки не трогаем."""
    CSS = """
    EpicManageScreen { align: center middle; }
    #dlg { width: 72; max-width: 92%; height: auto; padding: 1 2;
           border: round $accent; background: $surface; }
    #dlg Label { margin-bottom: 1; }
    #dlg Button { margin: 0 2 0 0; min-width: 22; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, key):
        super().__init__()
        self.key = key

    def compose(self):
        with Vertical(id="dlg"):
            yield Label("[b]Эпик %s[/b]" % self.key)
            yield Label("[dim]«Готово и убрать с доски»:\n  • в Jira переведём в Done сам эпик и ВСЕ его задачи (уже-Done пропустим)\n  • снимем локальные worktrees/ветки/папки задач\n  • эпик исчезнет из cc (история остаётся в Jira + git; remote MR/ветки целы)[/dim]")
            with Horizontal():
                yield Button("Готово и убрать с доски", id="done", variant="primary")
                yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self):
        self.dismiss(None)


class TaskManageScreen(ModalScreen):
    """Cleanup (local worktrees) or Abort (close MRs + delete remote branches + remove) a task."""
    CSS = """
    TaskManageScreen { align: center middle; }
    #dlg { width: 70; max-width: 92%; height: auto; padding: 1 2;
           border: round $accent; background: $surface; }
    #dlg Label { margin-bottom: 1; }
    #dlg Button { margin: 0 2 0 0; min-width: 14; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, tid):
        super().__init__()
        self.tid = tid

    def compose(self):
        with Vertical(id="dlg"):
            yield Label("[b]Задача %s[/b]" % self.tid)
            yield Label("[dim]«Готово и убрать с доски» = Jira задачи -> Done + снять локальные worktrees + убрать из cc (remote MR/ветка целы; откажет если незакоммичено).\nAbort = выбросить: закрыть MR + удалить remote-ветки + снести worktrees + убрать из cc.[/dim]")
            with Horizontal():
                yield Button("Готово и убрать с доски", id="done", variant="primary")
                yield Button("Abort (выбросить)", id="abort", variant="error")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self):
        self.dismiss(None)


class EpicTargetsScreen(ModalScreen):
    """Set per-repo integration branches (targets) for an epic — repo -> branch."""
    CSS = """
    EpicTargetsScreen { align: center middle; }
    #tbox { width: 94; max-width: 95%; height: auto; max-height: 90%;
            border: thick $accent; background: $surface; padding: 1 2; }
    #tbox > Label { margin-bottom: 1; }
    #tbody { height: auto; max-height: 22; overflow-y: auto; margin-bottom: 1; }
    .trow { height: 3; }
    .trow Label { width: 24; content-align: left middle; }
    .trow Input { width: 1fr; }
    #trbtn { height: auto; align-horizontal: right; }
    #trbtn Button { margin: 0 0 0 2; min-width: 14; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, epic):
        super().__init__()
        self.epic = epic
        self.project = cc.load_state()["epics"][epic]["project"]

    def compose(self):
        s = cc.load_state()
        targets = s["epics"][self.epic].get("targets") or {}
        with Vertical(id="tbox"):
            yield Label("[b]Интеграционные ветки[/b] эпика %s — repo → ветка (пусто = не в скоупе эпика)" % self.epic)
            yield Label("[dim]задачи будут базироваться на этих ветках и MR-иться в них; пусто → дефолтная ветка репо[/dim]")
            with VerticalScroll(id="tbody"):
                for r in s["projects"][self.project]["repos"]:
                    with Horizontal(classes="trow"):
                        yield Label(r)
                        yield Input(value=targets.get(r, ""), placeholder="напр. loyalty/integration", id="tgt__" + r)
            with Horizontal(id="trbtn"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="success", id="ok")

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None); return
        s = cc.load_state()
        pairs = {}
        for r in s["projects"][self.project]["repos"]:
            v = self.query_one("#tgt__" + r, Input).value.strip()
            if v:
                pairs[r] = v
        self.dismiss({"targets": pairs})

    def action_cancel(self):
        self.dismiss(None)


class CCApp(App):
    CSS = """
    Tree { width: 44%; border-right: solid $accent; }
    #detail { padding: 1 2; }
    """
    BINDINGS = [
        Binding("a", "add_project", "+Project"),
        Binding("R", "reviewers", "Reviewers"),
        Binding("D", "deploys", "Deploys"),
        Binding("T", "targets", "Targets"),
        Binding("j", "project_jira", "Jira/settings"),
        Binding("e", "new_epic", "+Epic"),
        Binding("n", "new_task", "+Task"),
        Binding("o", "open", "Chat/Setup"),
        Binding("O", "epic_chat", "Epic chat"),
        Binding("v", "view_chat", "View"),
        Binding("c", "cursor", "Cursor"),
        Binding("d", "diff", "Diff"),
        Binding("m", "mr", "MR dry"),
        Binding("M", "mr_real", "MR!"),
        Binding("g", "mrs", "MR links"),
        Binding("x", "cleanup", "Cleanup/Epic"),
        Binding("r", "refresh", "Refresh"),
        Binding("U", "recover", "Recover lost"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("cc", id="tree")
            with VerticalScroll():
                yield Static("", id="detail")
        yield Footer()

    def on_mount(self):
        self.title = "cc — multi-repo orchestrator"
        self._detail_urls = []
        self._busy = set()           # daemon-thread dedup (replaces run_worker exclusive=)
        self._changes = {}           # tid -> {repo: git-status text}; lazily filled, never on every focus
        self._changes_timer = None   # debounce handle for the focus -> lazy-fetch
        self._epic_synced = set()    # epics whose master-MRs we've already looked up this session
        self._epic_timer = None      # debounce handle for the epic-focus -> MR lookup
        self._collapsed = set()      # node keys ("project:NAME"/"epic:KEY") the user collapsed — kept across rebuilds
        self._building = False       # True while build_tree runs, so programmatic expand/collapse isn't recorded
        # Launch with a tidy, fully-collapsed board: seed every existing project & epic as collapsed
        # so the tree opens closed. The user drills into what they want (expansions are remembered for
        # the session); items created later this session aren't seeded, so they appear expanded/visible.
        try:
            s0 = cc.load_state()
            for _pn in s0.get("projects", {}):
                self._collapsed.add("project:" + _pn)
            for _ek in s0.get("epics", {}):
                self._collapsed.add("epic:" + _ek)
        except Exception:
            pass
        self.build_tree()
        try:
            n = len(cc._scan_orphans(cc.load_state()))
            if n:
                self.notify("⚠️ %d задач(и) на диске нет на доске — нажми U чтобы восстановить" % n,
                            severity="warning", timeout=10)
        except Exception:
            pass
        self.set_interval(8.0, self.refresh_glyphs)      # cheap (pid-based fast_status, no git)
        self.set_interval(90.0, self.kick_sync)
        self.set_interval(15.0, self.kick_statuses)      # full status (git) on a daemon thread
        self.kick_statuses()

    def _bg(self, name, fn):
        """Run fn on a *daemon* thread. Daemon threads are abandoned at interpreter exit, so a
        slow git/glab call can never keep cc tui from quitting (the old run_worker threads ran on
        asyncio's executor, which joins for THREAD_JOIN_TIMEOUT=300s on close). Deduped by name."""
        if self._closing or name in self._busy:
            return
        self._busy.add(name)
        def runner():
            try:
                fn()
            except Exception:
                pass
            finally:
                self._busy.discard(name)
        threading.Thread(target=runner, name="cc-" + name, daemon=True).start()

    def _daemon(self, fn):
        """Fire-and-forget on a daemon thread (no dedup). Used by modal loaders so their glab/Jira
        network calls never run on asyncio's default executor either — if that executor is never
        used, asyncio.run()'s shutdown_default_executor returns instantly and q can't hang."""
        if self._closing:
            return
        def runner():
            try:
                fn()
            except Exception:
                pass
        threading.Thread(target=runner, name="cc-load", daemon=True).start()

    def _safe_build_tree(self):
        if self._closing:
            return
        try:
            self.call_from_thread(self.build_tree)
        except Exception:
            pass

    def action_open_url_idx(self, idx):
        try:
            url = self._detail_urls[int(idx)]
            subprocess.Popen(["open", url])
            self.notify("открыл %s" % url)
        except Exception:
            self.notify("ссылка недоступна", severity="error")

    def kick_sync(self):
        self._bg("mrsync", self._sync_mrs)

    def kick_statuses(self):
        self._bg("statuses", self._refresh_statuses)

    def _refresh_statuses(self):
        # Off-thread status sweep over the (small, active) board: full git-based task_status for
        # every non-running task, so a merged task you re-touched flips back to review, and new
        # finishes surface. Runs on a daemon thread; the board stays small because finished work is
        # removed via "Готово и убрать с доски", so this isn't the per-focus git storm we killed.
        snap = cc.load_state()
        new = {}
        for tid, t in snap["tasks"].items():
            if self._closing:
                return
            pid = t.get("pid")
            new[tid] = "running" if (pid and cc.pid_alive(pid)) else cc.task_status(t)
        if self._closing:
            return
        if all(snap["tasks"][tid].get("status") == st for tid, st in new.items()):
            return
        def apply(s):
            for tid, st in new.items():
                if tid in s["tasks"]:
                    s["tasks"][tid]["status"] = st
        # best-effort: never block/starve a foreground `cc` command; retry next tick if busy
        if cc.mutate_try(apply):
            self._safe_build_tree()

    def _sync_mrs(self):
        st = cc.load_state()
        tids = [tid for tid, t in st["tasks"].items() if t.get("mrs") and not t.get("merged")]
        for tid in tids:
            if self._closing:
                return
            try:
                subprocess.run([sys.executable, ENGINE, "task", "mrs", tid], capture_output=True, timeout=15)
            except Exception:
                pass
        if tids:
            self._safe_build_tree()

    def state(self):
        return cc.load_state()

    # flat ordering under an epic: needs-you first, finished last
    TASK_ORDER = {"review": 0, "running": 1, "mr": 2, "idle": 3, "merged": 4}

    def _unseen(self, t):
        """Agent finished and wrote output you haven't opened yet."""
        log = t.get("log")
        try:
            return bool(log) and os.path.exists(log) and os.path.getmtime(log) > t.get("seen_at", 0)
        except Exception:
            return False

    def _add_epic_node(self, parent, ekey, e, s, expand=True):
        en = parent.add("# %s  %s" % (ekey, e.get("summary", "")),
                        data={"type": "epic", "id": ekey}, expand=expand)
        # flat list of this epic's tasks, sorted needs-you -> finished; finished is dimmed.
        tasks = [(tid, t) for tid, t in s["tasks"].items() if t["epic"] == ekey]
        # primary: status hierarchy (ждут->в работе->на ревью->готово);
        # secondary: most-recently-completed first within a group (recently-closed at the top)
        tasks.sort(key=lambda it: (self.TASK_ORDER.get(fast_status(it[1]), 0), -(it[1].get("merged_at") or 0)))
        for tid, t in tasks:
            st = fast_status(t)
            badge = "💬 " if (st == "review" and self._unseen(t)) else ""
            row = "%s%s %s" % (badge, GLYPH.get(st, "?"), t["title"])
            en.add_leaf("[dim]%s[/dim]" % row if st == "merged" else row,
                        data={"type": "task", "id": tid})
        if not tasks:
            en.add_leaf("[dim](нет задач — n чтобы добавить)[/dim]", data=None)
        return en

    @staticmethod
    def _node_key(data):
        """Stable identity for a tree node across rebuilds (for collapse-state + cursor restore)."""
        if not data:
            return None
        t = data.get("type")
        if t in ("project", "epic", "task"):
            return "%s:%s" % (t, data["id"])
        if t == "orphans":
            return "orphans:"
        return None

    def _find_node(self, node, key):
        for ch in node.children:
            if self._node_key(ch.data) == key:
                return ch
            found = self._find_node(ch, key)
            if found is not None:
                return found
        return None

    def build_tree(self):
        tree = self.query_one("#tree", Tree)
        prev_key = self._node_key(self.current())   # remember focused node so the cursor survives the rebuild
        self._building = True                        # programmatic expand/collapse below must NOT touch _collapsed
        try:
            tree.clear()
            s = self.state()
            tree.root.expand()
            for pname, p in s["projects"].items():
                pkey = "project:" + pname
                pn = tree.root.add("%s  [%d repo]" % (pname, len(p["repos"])),
                                   data={"type": "project", "id": pname},
                                   expand=(pkey not in self._collapsed))
                epics = [(k, e) for k, e in s["epics"].items()
                         if e["project"] == pname and not e.get("archived")]
                for ekey, e in epics:
                    self._add_epic_node(pn, ekey, e, s, expand=(("epic:" + ekey) not in self._collapsed))
                if not epics:
                    pn.add_leaf("(no epics — press 'e' to add one)", data=None)
            orphans = cc._scan_orphans(s)
            if orphans:
                on = tree.root.add("⚠️  Потеряшки на диске (%d) — U чтобы восстановить" % len(orphans),
                                   data={"type": "orphans"}, expand=True)
                for o in orphans:
                    on.add_leaf("[yellow]%s / %s  (ветка %s, %d репо)[/yellow]"
                                % (o["epic"], o["slug"], o["branch"], len(o["repos"])), data=None)
        finally:
            self._building = False
        # Restore the cursor AFTER the tree re-renders: a freshly-added node has no line index yet
        # (TreeNode.line == -1 until layout), so move_cursor() must wait for the refresh pass.
        self._restore_key = prev_key
        if prev_key:
            self.call_after_refresh(self._restore_cursor)

    def _restore_cursor(self):
        key = getattr(self, "_restore_key", None)
        if not key:
            return
        self._restore_key = None
        tree = self.query_one("#tree", Tree)
        node = self._find_node(tree.root, key)
        if node is not None:
            try:
                tree.move_cursor(node)
            except Exception:
                pass

    def on_tree_node_collapsed(self, event):
        if self._building:
            return
        k = self._node_key(event.node.data if event.node else None)
        if k and k.split(":", 1)[0] in ("project", "epic"):
            self._collapsed.add(k)

    def on_tree_node_expanded(self, event):
        if self._building:
            return
        k = self._node_key(event.node.data if event.node else None)
        if k:
            self._collapsed.discard(k)

    def current(self):
        node = self.query_one("#tree", Tree).cursor_node
        return node.data if node and node.data else None

    def on_tree_node_highlighted(self, event):
        data = event.node.data if event.node else None
        self.show_detail(data)
        # First time you rest on a task, fetch its working-tree changes ONCE (debounced, off-thread)
        # and cache them. We never re-shell git on subsequent focuses — only `r` re-fetches. This is
        # what used to lag: git status ran for every repo on every cursor move across the tree.
        if data and data.get("type") == "task" and data["id"] not in self._changes:
            self._schedule_changes(data["id"])
        elif data and data.get("type") == "epic" and data["id"] not in self._epic_synced:
            self._schedule_epic_mrs(data["id"])

    def _schedule_changes(self, tid):
        if self._changes_timer is not None:
            try:
                self._changes_timer.stop()
            except Exception:
                pass
        self._changes_timer = self.set_timer(0.35, lambda: self._fetch_changes(tid))

    def _fetch_changes(self, tid, force=False):
        if force:
            self._changes.pop(tid, None)
        self._bg("changes:" + tid, lambda: self._compute_changes(tid))

    def _compute_changes(self, tid):
        snap = cc.load_state()
        t = snap["tasks"].get(tid)
        if not t:
            return
        res = {}
        for r in t.get("repos", []):
            if self._closing:
                return
            wt = t.get("worktrees", {}).get(r)
            res[r] = cc._changed(wt) if wt else ""
        self._changes[tid] = res
        if self._closing:
            return
        def upd():
            cur = self.current()
            if cur and cur.get("type") == "task" and cur.get("id") == tid:
                self.show_detail(cur)
        try:
            self.call_from_thread(upd)
        except Exception:
            pass

    def _schedule_epic_mrs(self, ekey):
        if self._epic_timer is not None:
            try:
                self._epic_timer.stop()
            except Exception:
                pass
        self._epic_timer = self.set_timer(0.4, lambda: self._fetch_epic_mrs(ekey))

    def _fetch_epic_mrs(self, ekey, force=False):
        if force:
            self._epic_synced.discard(ekey)
        if ekey in self._epic_synced:
            return
        self._epic_synced.add(ekey)
        self._bg("epicmrs:" + ekey, lambda: self._compute_epic_mrs(ekey))

    def _compute_epic_mrs(self, ekey):
        # `cc epic mrs` queries GitLab for each repo's epic->master MR and saves it to state
        try:
            subprocess.run([sys.executable, ENGINE, "epic", "mrs", ekey], capture_output=True, timeout=90)
        except Exception:
            pass
        if self._closing:
            return
        def upd():
            self.build_tree()
            cur = self.current()
            if cur and cur.get("type") == "epic" and cur.get("id") == ekey:
                self.show_detail(cur)
        try:
            self.call_from_thread(upd)
        except Exception:
            pass

    def _after_epic_sync(self, ekey):
        self._epic_synced.add(ekey)
        self.build_tree()
        cur = self.current()
        if cur:
            self.show_detail(cur)

    def show_detail(self, data):
        d = self.query_one("#detail", Static)
        self._detail_urls = []
        if not data:
            d.update("")
            return
        s = self.state()
        if data["type"] in ("taskgroup", "jiragroup"):
            d.update("[dim]группа задач — раскрой и выбери задачу[/dim]")
            return
        if data["type"] == "jira_stub":
            st = "Done" if data.get("done") else "не начата"
            d.update("[b]%s[/b]  (Jira — %s)\n%s\n\n[dim]n — активировать: создаст cc-задачу (worktree+агент), prompt засеян из Jira[/dim]"
                     % (data["key"], st, data.get("summary", "")))
            return
        if data["type"] == "archive":
            n = sum(1 for e in s["epics"].values() if e["project"] == data["id"] and e.get("archived"))
            d.update("[b]Архив[/b] — %d архивн. эпик(ов) в '%s'\n[dim]разверни узел; на эпике x → Разархивировать[/dim]" % (n, data["id"]))
            return
        if data["type"] == "task" and data["id"] in s["tasks"]:
            t = s["tasks"][data["id"]]
            L = ["[b]%s[/b]   status=%s" % (t["title"], fast_status(t)),
                 "epic: %s    branch: %s" % (t["epic"], t["branch"]), "", "repos -> target:"]
            for r in t["repos"]:
                L.append("  %s -> %s" % (r, t["base"][r]))
                if t["mrs"].get(r):
                    url = t["mrs"][r]
                    i = len(self._detail_urls)
                    self._detail_urls.append(url)
                    L.append("     [@click=app.open_url_idx(%d)][u]MR: %s[/u][/]" % (i, url))
            if t.get("merged"):
                L += ["", "[bold green]✓ все MR влиты — x = очистить worktrees[/bold green]"]
            L += ["", "[b]changes:[/b]"]
            cached = self._changes.get(data["id"])
            if cached is None:
                L.append("  [dim]…(r — показать локальные изменения)[/dim]")
            else:
                for r in t["repos"]:
                    st = cached.get(r, "")
                    L.append("  [%s] %s" % (r, st.replace("\n", "; ") if st else "(none)"))
            L += ["", "[dim]o=chat v=view c=cursor d=diff m=dry M=create g=links x=cleanup[/dim]"]
            d.update("\n".join(L))
        elif data["type"] == "epic":
            e = s["epics"][data["id"]]
            mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
            mrs = e.get("mrs", {}) or {}
            mst = e.get("mr_state", {}) or {}
            L = ["[b]epic %s[/b]  %s   [%s]" % (data["id"], e.get("summary", ""), mode), ""]
            if mode == "epic_branch":
                L += ["ветка эпика: [b]%s[/b]  ->  master/main" % data["id"]]
                rows = [(r, data["id"]) for r in sorted(mrs)]
            else:
                L += ["routing (интеграционные ветки):"]
                for r, b in e.get("targets", {}).items():
                    L.append("  %s -> %s" % (r, b))
                rows = list((e.get("targets") or {}).items())
            # epic -> master/main MRs (clickable, lazily filled on focus / by g; merged shown too)
            L += ["", "[b]MR в master/main:[/b]"]
            COLOR = {"opened": "green", "merged": "magenta", "closed": "red", "locked": "yellow"}
            shown = 0
            for r, br in rows:
                url = mrs.get(r)
                if url:
                    idx = len(self._detail_urls)
                    self._detail_urls.append(url)
                    st = mst.get(r, "") or "open"
                    c = COLOR.get(mst.get(r, ""), "white")
                    tail = ("  [dim]<- %s[/dim]" % br) if mode == "targets" else ""
                    L.append("  [@click=app.open_url_idx(%d)][u]%-20s[/u][/]  [%s]%s[/%s]%s" % (idx, r, c, st, c, tail))
                    shown += 1
                elif mode == "targets":
                    L.append("  %-20s  [dim]- MR не найден (<- %s)[/dim]" % (r, br))
            if not shown:
                hint = "[dim]…(g — найти MR в master)[/dim]" if data["id"] not in self._epic_synced else "[dim]- MR в master пока нет[/dim]"
                L += ["  " + hint]
            L += ["", "[dim]n=задача  M=MR эпик->master  m=dry  g=обновить MR-ссылки[/dim]"]
            d.update("\n".join(L))
        elif data["type"] == "project":
            p = s["projects"][data["id"]]
            j = p.get("jira", {})
            jira_line = ("on — %s @ %s" % (j.get("project_key", "?"), j.get("site", "?"))) if j.get("token") else "off"
            L = ["[b]project %s[/b]  (%s, %d repos)" % (data["id"], p["kind"], len(p["repos"])),
                 "assignee: %s    Jira: %s" % (p.get("default_assignee") or "-", jira_line),
                 "", "repos (reviewer):"]
            L += ["repos — deploy / reviewer:"]
            for r, ri in p["repos"].items():
                dep = ri.get("deploy", {})
                if dep.get("kind") == "eas":
                    ch = dep.get("channels", {})
                    env = "[cyan]EAS[/cyan] staging:[green]%s[/green]  prod:[yellow]%s[/yellow]" % (
                        ch.get("staging", "?"), ch.get("production", "?"))
                elif dep.get("kind") == "gitlab":
                    e = dep.get("envs", {})
                    def _sr(k):
                        v = (e.get(k) or {}).get("ref")
                        if not v:
                            return "[dim]—[/dim]"
                        m = re.match(r"refs/merge-requests/(\d+)/head", v)
                        return "!" + m.group(1) if m else v
                    env = "dev:%s  stage:[green]%s[/green]  prod:[yellow]%s[/yellow]" % (
                        _sr("dev"), _sr("stage"), _sr("prod"))
                else:
                    env = "[dim](D = загрузить деплои)[/dim]"
                L.append("  %-20s %s  ·  rev:%s" % (r, env, ri.get("reviewer") or "—"))
            L += ["", "[dim]e=эпик  R=ревьюеры  j=Jira  D=обновить деплои[/dim]"]
            d.update("\n".join(L))

    def refresh_glyphs(self):
        s = self.state()
        # cheap edge-detect: an agent that just finished still has pid set + cached "running",
        # but the process is dead. Fire one status refresh so it moves to "🟡 Ждут тебя" within
        # ~8s instead of waiting for the 30s tick — without polling git every second in steady state.
        if any(t.get("pid") and not cc.pid_alive(t["pid"]) and t.get("status") == "running"
               for t in s["tasks"].values()):
            self.kick_statuses()

        def walk(node):
            for ch in node.children:
                dd = ch.data
                if dd and dd.get("type") == "task" and dd["id"] in s["tasks"]:
                    t = s["tasks"][dd["id"]]
                    ch.set_label("%s %s" % (GLYPH.get(fast_status(t), "?"), t["title"]))
                walk(ch)
        walk(self.query_one("#tree", Tree).root)

    def action_recover(self):
        # restore tasks whose work is on disk but missing from the board
        self.push_screen(OutputScreen("recover lost tasks",
                         [sys.executable, "-u", ENGINE, "recover"]),
                         lambda _: self.build_tree())

    def action_refresh(self):
        cur = self.current()
        # `r` on an EPIC node = re-sync its children from Jira
        if cur and cur.get("type") == "epic":
            ekey = cur["id"]
            s = self.state()
            proj = s["epics"].get(ekey, {}).get("project")
            if s["projects"].get(proj, {}).get("jira", {}).get("token"):
                self.notify("синхронизирую задачи эпика из Jira …")
                self._bg("esync:%s" % ekey, lambda: self._sync_epic(ekey))
                return
        # `r` on a TASK node = re-probe just that task's working-tree changes (the only on-demand git)
        if cur and cur.get("type") == "task":
            self._fetch_changes(cur["id"], force=True)
        self.kick_statuses()
        self.build_tree()
        self.notify("refreshed")

    def _sync_epic(self, ekey):
        subprocess.run([sys.executable, ENGINE, "epic", "sync", ekey], capture_output=True)
        self.call_from_thread(self.build_tree)
        self.call_from_thread(lambda: self.notify("задачи эпика обновлены"))

    # ---- project/epic resolution from current selection ----
    def _current_project(self):
        s = self.state()
        data = self.current()
        if data:
            if data["type"] == "project":
                return data["id"]
            if data["type"] == "epic":
                return s["epics"][data["id"]]["project"]
            if data["type"] == "task":
                return s["epics"][s["tasks"][data["id"]]["epic"]]["project"]
        if len(s["projects"]) == 1:
            return list(s["projects"])[0]
        return None

    def _current_epic(self):
        s = self.state()
        data = self.current()
        if data:
            if data["type"] == "epic":
                return data["id"]
            if data["type"] == "task":
                return s["tasks"][data["id"]]["epic"]
            if data["type"] == "jira_stub":
                return data["epic"]
            if data["type"] in ("taskgroup", "jiragroup"):
                return data["epic"]
        return None

    # ---- create epic ----
    def action_add_project(self):
        self.push_screen(AddProjectScreen(), self._project_added)

    def action_reviewers(self):
        proj = self._current_project()
        if not proj:
            self.notify("select a project node first", severity="error"); return
        self.push_screen(ReviewersScreen(proj))

    def action_targets(self):
        ekey = self._current_epic()
        if not ekey:
            self.notify("выбери эпик (или его задачу)", severity="error"); return
        self.push_screen(EpicTargetsScreen(ekey), lambda res: self._targets_set(ekey, res))

    def _targets_set(self, ekey, res):
        if not res:
            return
        pairs = res["targets"]
        args = [sys.executable, ENGINE, "epic", "set", ekey]
        if pairs:
            args += ["--repos", ",".join(pairs)]
            for r, b in pairs.items():
                args += ["--target", "%s=%s" % (r, b)]
        subprocess.run(args, capture_output=True)
        self.build_tree()
        ntasks = sum(1 for t in self.state()["tasks"].values() if t.get("epic") == ekey)
        if ntasks:
            self.notify("targets сохранены. %d задач(а) на старой базе — пересоздай (x→abort + заново активируй стаб)" % ntasks)
        else:
            self.notify("targets для %s сохранены" % ekey)

    def action_deploys(self):
        proj = self._current_project()
        if not proj:
            self.notify("выбери проект", severity="error"); return
        self.notify("обновляю статусы деплоя (dev/stage/prod) …")
        self._bg("deploys:%s" % proj, lambda: self._do_deploys(proj))

    def _do_deploys(self, proj):
        subprocess.run([sys.executable, ENGINE, "deploys", proj], capture_output=True)
        self.call_from_thread(self.build_tree)
        self.call_from_thread(lambda: self.notify("деплои обновлены"))

    def action_project_jira(self):
        proj = self._current_project()
        if not proj:
            self.notify("select a project node first", severity="error"); return
        self.push_screen(ProjectJiraScreen(proj), lambda _: self.build_tree())

    def _project_added(self, res):
        if not res:
            return
        args = [sys.executable, ENGINE, "project", "add", res["path"]]
        if res["name"]:
            args.append(res["name"])
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            self.notify("project add failed: %s" % r.stderr.strip()[:120], severity="error")
        else:
            line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else "project added"
            self.notify(line)
        self.build_tree()

    def action_new_epic(self):
        proj = self._current_project()
        if not proj:
            self.notify("select a project node first", severity="error")
            return
        jira_on = bool(self.state()["projects"][proj].get("jira", {}).get("token"))
        self.push_screen(NewEpicScreen(proj, jira_on), self._epic_created)

    def _epic_created(self, res):
        if not res:
            return
        proj = self._current_project() or list(self.state()["projects"])[0]
        if res.get("action") == "pick":
            r = subprocess.run([sys.executable, ENGINE, "epic", "add", proj, res["key"],
                                "--summary", res.get("summary", "")], capture_output=True, text=True)
            self.notify("эпик %s взят из Jira" % res["key"] if r.returncode == 0
                        else "ошибка: %s" % r.stderr.strip()[:100])
        elif res.get("action") == "create":
            r = subprocess.run([sys.executable, ENGINE, "jira", "create-epic", proj, res["summary"]],
                               capture_output=True, text=True)
            m = re.search(r"created Jira epic: (\S+)", r.stdout or "")
            if m:
                subprocess.run([sys.executable, ENGINE, "epic", "add", proj, m.group(1),
                                "--summary", res["summary"]], capture_output=True, text=True)
                self.notify("создан Jira-эпик %s" % m.group(1))
            else:
                self.notify("не создал Jira-эпик: %s" % ((r.stderr or r.stdout).strip()[:120]), severity="error")
        else:
            args = [sys.executable, ENGINE, "epic", "add", proj, res["key"]]
            if res.get("summary"):
                args += ["--summary", res["summary"]]
            for pair in (res.get("targets", "") or "").split(","):
                pair = pair.strip()
                if pair:
                    args += ["--target", pair]
            if res.get("repos"):
                args += ["--repos", res["repos"]]
            r = subprocess.run(args, capture_output=True, text=True)
            self.notify("epic %s added" % res["key"] if r.returncode == 0
                        else "epic failed: %s" % r.stderr.strip()[:120])
        self.build_tree()

    # ---- create task ----
    def action_new_task(self):
        data = self.current()
        s = self.state()
        if data and data["type"] == "jira_stub":
            ekey = data["epic"]; proj = s["epics"][ekey]["project"]
            self.push_screen(NewTaskScreen(ekey, jira_on=True, project=proj,
                             preset_key=data["key"], preset_summary=data.get("summary", "")),
                             self._task_created)
            return
        epic = self._current_epic()
        if not epic:
            self.notify("select an epic (or its task) first", severity="error")
            return
        proj = s["epics"].get(epic, {}).get("project")
        jira_on = bool(s["projects"].get(proj, {}).get("jira", {}).get("token"))
        self.push_screen(NewTaskScreen(epic, jira_on=jira_on, project=proj), self._task_created)

    def _task_created(self, res):
        if not res:
            return
        # provisioning N worktrees + Jira + agent launch takes 10-30s; stream it in a
        # worker (OutputScreen) so the TUI doesn't freeze and progress/errors are visible.
        args = [sys.executable, "-u", ENGINE, "task", "add", res["epic"], res["title"],
                "--prompt", res["prompt"]]
        if res.get("jira"):
            args += ["--jira", res["jira"]]
        self.push_screen(OutputScreen("task: %s" % res["title"], args),
                         lambda _: self.build_tree())

    # ---- task actions ----
    def _cur_task(self):
        data = self.current()
        if not data or data["type"] != "task":
            self.notify("select a task first")
            return None
        return data["id"]

    def _pane_alive(self, pane):
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True).stdout
        return pane in out.split()

    def _chat_cmd(self, t):
        cwd = t.get("dir") or t["worktrees"][t["primary"]]
        sid = t.get("claude_session", {}).get(t["primary"]) or cc.resolve_session(cwd)
        return cwd, ("claude --resume %s --permission-mode auto" % sid if sid else "claude --permission-mode auto")

    def _mark_seen(self, tid):
        now = time.time()
        def _set(s):
            if tid in s["tasks"]:
                s["tasks"][tid]["seen_at"] = now
        cc.mutate(_set)

    def action_view_chat(self):
        tid = self._cur_task()
        if not tid:
            return
        self._mark_seen(tid)
        self.push_screen(ChatScreen(tid))

    def _open_chat_tab(self, name, cwd, cmd):
        """Open the chat in a NEW cmux tab (native trackpad scroll + Cmd-W to close)."""
        cm = _cmux_path()
        if not cm:
            open_in_terminal(cwd, cmd); return "terminal"
        r = subprocess.run([cm, "new-surface", "--type", "terminal", "--focus", "true"],
                           capture_output=True, text=True)
        m = re.search(r"surface:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", (r.stdout or "") + (r.stderr or ""))
        ref = m.group(0) if m else None
        if not ref:
            open_in_terminal(cwd, cmd); return "terminal"
        subprocess.run([cm, "send", "--surface", ref, "--", "cd %s && %s\n" % (shlex.quote(cwd), cmd)],
                       capture_output=True)
        subprocess.run([cm, "rename-tab", "--surface", ref, name], capture_output=True)
        return "cmux"

    def action_open(self):
        data = self.current()
        if data and data.get("type") == "project":
            self.action_project_setup(); return
        tid = self._cur_task()
        if not tid:
            self.notify("встань на задачу (o=чат), эпик (O), или проект (o=настройка под ИИ)")
            return
        t = self.state()["tasks"][tid]
        cwd = t.get("dir") or t["worktrees"][t["primary"]]
        if not os.path.isdir(cwd):
            self.notify("worktree missing — recreate the task", severity="error"); return
        self._mark_seen(tid)
        sid = (t.get("claude_session") or {}).get(t["primary"]) or cc.resolve_session(cwd)
        chat = "claude --resume %s --permission-mode auto" % sid if sid else "claude --permission-mode auto"
        where = self._open_chat_tab("cc:%s" % tid, cwd, chat)
        self.notify("чат %s открыт в новой вкладке (%s); Cmd-W закрыть" % (tid, where))

    def action_project_setup(self):
        proj = self._current_project()
        if not proj:
            self.notify("выбери проект", severity="error"); return
        subprocess.run([sys.executable, ENGINE, "project", "setup", proj], capture_output=True)
        base = self.state()["projects"].get(proj, {}).get("path", "")
        sdir = os.path.join(base, ".cc-setup")
        if not os.path.isdir(sdir):
            self.notify("не удалось подготовить setup-чат", severity="error"); return
        sid = cc.resolve_session(sdir)   # resume the SAME session if one already exists for this chat
        resume = ("--resume %s " % sid) if sid else ""
        cmd = "claude %s--permission-mode auto --add-dir %s" % (resume, shlex.quote(base))
        where = self._open_chat_tab("cc:setup %s" % proj, sdir, cmd)
        self.notify("чат проекта '%s' %s (%s)" % (proj, "продолжен" if sid else "открыт", where))

    def action_epic_chat(self):
        ekey = self._current_epic()
        if not ekey:
            self.notify("выбери эпик (или его задачу)", severity="error"); return
        subprocess.run([sys.executable, ENGINE, "epic", "open", ekey], capture_output=True)
        s = self.state()
        e = s["epics"].get(ekey, {})
        proj = s["projects"].get(e.get("project"), {})
        edir = os.path.join(proj.get("path", ""), "cctui", ekey, "_release")
        if not os.path.isdir(edir):
            self.notify("не удалось подготовить чат эпика", severity="error"); return
        repos = e.get("repos") or list(proj.get("repos", {}).keys())
        adds = " ".join("--add-dir %s" % shlex.quote(proj["repos"][r]["path"])
                        for r in repos if proj.get("repos", {}).get(r, {}).get("path"))
        sid = cc.resolve_session(edir)   # resume the SAME epic chat session if it exists
        resume = ("--resume %s " % sid) if sid else ""
        cmd = "claude %s--permission-mode auto %s" % (resume, adds)
        where = self._open_chat_tab("cc:epic %s" % ekey, edir, cmd)
        self.notify("чат эпика %s %s (%s) — релиз/координация" % (ekey, "продолжен" if sid else "открыт", where))

    def action_cursor(self):
        tid = self._cur_task()
        if not tid:
            return
        subprocess.run([sys.executable, ENGINE, "task", "open", tid], capture_output=True)
        t = self.state()["tasks"].get(tid, {})
        folder = t.get("dir")
        if shutil.which("cursor") and folder and os.path.isdir(folder):
            subprocess.Popen(["cursor", folder])
            self.notify("Cursor открыт (папка задачи) для %s" % tid)
        else:
            self.notify("cursor CLI или папка задачи отсутствует", severity="error")

    def action_diff(self):
        tid = self._cur_task()
        if not tid:
            return
        self.push_screen(DiffScreen(tid))

    def action_mr(self):
        self._mr(True)

    def action_mr_real(self):
        self._mr(False)

    def action_mrs(self):
        data = self.current()
        if data and data["type"] == "epic":
            ekey = data["id"]
            self.push_screen(OutputScreen("Epic MR links: %s" % ekey,
                             [sys.executable, "-u", ENGINE, "epic", "mrs", ekey]),
                             lambda _: self._after_epic_sync(ekey))
        elif data and data["type"] == "task":
            self.push_screen(OutputScreen("MR links: %s" % data["id"],
                             [sys.executable, "-u", ENGINE, "task", "mrs", data["id"]]),
                             lambda _: self.build_tree())
        else:
            self.notify("выбери задачу или эпик")

    def action_cleanup(self):
        data = self.current()
        if data and data["type"] == "epic":
            ekey = data["id"]
            self.push_screen(EpicManageScreen(ekey), lambda res: self._epic_manage(ekey, res))
            return
        tid = self._cur_task()
        if not tid:
            self.notify("встань на задачу (Готово/Abort) или эпик (Готово и убрать с доски)")
            return
        self.push_screen(TaskManageScreen(tid), lambda res: self._task_manage(tid, res))

    def _task_manage(self, tid, res):
        if not res:
            return
        if res == "done":
            self.push_screen(OutputScreen("Готово и убрать: %s" % tid,
                             [sys.executable, "-u", ENGINE, "task", "done", tid]),
                             lambda _: self.build_tree())
        elif res == "abort":
            self.push_screen(OutputScreen("abort: %s" % tid,
                             [sys.executable, "-u", ENGINE, "task", "abort", tid]),
                             lambda _: self.build_tree())

    def _epic_manage(self, ekey, res):
        if res != "done":
            return
        # Jira(эпик+задачи)->Done + локальная уборка эпика и задач из cc — стримим вывод
        self.push_screen(OutputScreen("Готово и убрать: %s (+ Jira Done)" % ekey,
                         [sys.executable, "-u", ENGINE, "epic", "done", ekey]),
                         lambda _: self.build_tree())

    def _mr(self, dry):
        data = self.current()
        if not data:
            self.notify("select a task or epic")
            return
        if data["type"] == "task":
            argv = [sys.executable, "-u", ENGINE, "task", "mr", data["id"]]; title = "MR: %s" % data["id"]
        elif data["type"] == "epic":
            argv = [sys.executable, "-u", ENGINE, "epic", "mr", data["id"]]; title = "Epic MR: %s" % data["id"]
        else:
            self.notify("select a task (MR) or epic (epic->master MR)")
            return
        if dry:
            argv.append("--dry-run"); title += " (dry)"
        ekey = data["id"] if data["type"] == "epic" else None
        self.push_screen(OutputScreen(title, argv),
                         lambda _: (self._after_epic_sync(ekey) if ekey else self.build_tree()))


if __name__ == "__main__":
    CCApp().run()
