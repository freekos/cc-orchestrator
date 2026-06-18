#!/usr/bin/env python3
"""cc TUI — terminal interface over the cc engine (Phase 3, Textual)."""
import json, os, re, shlex, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cc

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Tree, Static, Input, Button, Label, TextArea, Select
from textual.binding import Binding

ENGINE = str(Path(__file__).parent / "cc.py")
GLYPH = {"running": "~", "review": "*", "mr": "MR", "merged": "v", "idle": ".", "done": "v"}


class NewEpicScreen(ModalScreen):
    CSS = """
    NewEpicScreen { align: center middle; }
    #dlg { width: 82; max-width: 92%; height: auto; max-height: 90%; overflow-y: auto;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg Label { margin-bottom: 1; }
    #dlg Input { margin-bottom: 1; width: 1fr; }
    #dlg Select { margin-bottom: 1; width: 1fr; }
    #dlg Button { margin: 0 2 1 0; }
    #row { height: auto; }
    """

    def __init__(self, project, jira_on=False):
        super().__init__()
        self.project = project
        self.jira_on = jira_on
        self._epics = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            if self.jira_on:
                yield Label("Epic из Jira для '%s' — выбери свой эпик или создай новый" % self.project)
                yield Input(placeholder="поиск по названию (Enter)", id="esearch")
                yield Select([("(загрузка моих эпиков…)", "_")], id="epick", allow_blank=True)
                with Horizontal(id="row"):
                    yield Button("Взять выбранный", variant="success", id="use")
                    yield Button("Cancel", id="cancel")
                yield Label("— или создать новый эпик в Jira: —")
                yield Input(placeholder="название нового эпика", id="enew")
                yield Button("Создать в Jira", variant="primary", id="createnew")
            else:
                yield Label("New epic under project '%s'" % self.project)
                yield Input(placeholder="key (e.g. IK-8631)", id="key")
                yield Input(placeholder="summary", id="summary")
                yield Input(placeholder="targets repo=branch,... (EMPTY = epic gets its own branch)", id="targets")
                yield Input(placeholder="repos (optional, comma); empty = ALL", id="erepos")
                with Horizontal(id="row"):
                    yield Button("Create", variant="success", id="ok")
                    yield Button("Cancel", id="cancel")

    def on_mount(self):
        if self.jira_on:
            self.run_worker(lambda: self._load_epics(""), thread=True)

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
            opts = [("%s — %s" % (e["key"], e["summary"]), e["key"]) for e in self._epics]
            sel.set_options(opts or [("(эпиков не найдено)", "_")])
        except Exception:
            pass

    def on_input_submitted(self, event):
        if self.jira_on and event.input.id == "esearch":
            self.run_worker(lambda: self._load_epics(event.value.strip()), thread=True)

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
    #dlg { width: 90; max-width: 92%; height: auto; max-height: 90%; overflow-y: auto;
           border: thick $accent; background: $surface; padding: 1 2; }
    #dlg Label { margin-bottom: 1; }
    #dlg Input { margin-bottom: 1; width: 1fr; }
    #dlg Select { margin-bottom: 1; width: 1fr; }
    #dlg Button { margin: 0 2 1 0; }
    #dlg TextArea { height: 8; margin-bottom: 1; }
    #row { height: auto; }
    """

    def __init__(self, epic):
        super().__init__()
        self.epic = epic

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("New task under epic '%s'" % self.epic)
            yield Label("[dim]идёт по всем репо проекта; MR создаётся только там, где агент что-то менял[/dim]")
            yield Input(placeholder="title (e.g. fix badge)", id="title")
            yield Label("prompt:")
            yield TextArea(id="prompt")
            with Horizontal(id="row"):
                yield Button("Launch", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        title = self.query_one("#title", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not title or not prompt:
            self.app.notify("title and prompt are required", severity="error")
            return
        self.dismiss({"epic": self.epic, "title": title, "prompt": prompt})


def open_in_terminal(workdir, shell_cmd):
    """Open a NEW terminal tab/window running shell_cmd in workdir (keeps the TUI alive)."""
    full = "cd %s && %s" % (shlex.quote(workdir), shell_cmd)
    esc = full.replace("\\", "\\\\").replace('"', '\\"')
    term = os.environ.get("TERM_PROGRAM", "")
    if term == "iTerm.app":
        subprocess.Popen(["osascript",
            "-e", 'tell application "iTerm"',
            "-e", 'tell current window to create tab with default profile',
            "-e", 'tell current session of current window to write text "%s"' % esc,
            "-e", 'end tell'])
    else:
        subprocess.Popen(["osascript",
            "-e", 'tell application "Terminal" to do script "%s"' % esc,
            "-e", 'tell application "Terminal" to activate'])


CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def open_cmux(name, cwd, cmd):
    """Open the command as a NEW TAB (surface) in the CURRENT cmux workspace, then send it.
    Falls back to a new Terminal.app tab if cmux socket isn't reachable / ref can't be parsed."""
    cm = shutil.which("cmux") or (CMUX_BIN if os.path.exists(CMUX_BIN) else None)
    if cm:
        r = subprocess.run([cm, "new-surface", "--type", "terminal", "--focus", "true"],
                           capture_output=True, text=True)
        ref = None
        if r.returncode == 0:
            m = re.search(r"surface:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", (r.stdout or "") + (r.stderr or ""))
            ref = m.group(0) if m else None
        if ref:
            subprocess.run([cm, "send", "--surface", ref, "--",
                            "cd %s && %s\n" % (shlex.quote(cwd), cmd)], capture_output=True, text=True)
            subprocess.run([cm, "rename-tab", "--surface", ref, name], capture_output=True, text=True)
            return "cmux"
    open_in_terminal(cwd, cmd)
    return "terminal"


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
        chat = ("claude --resume %s" % sid) if sid else "claude"
        open_cmux("%s chat" % self.tid, pw, chat)
        self.app.notify("ответ: открыл интерактивный чат в новой вкладке")


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
        self.run_worker(self._run, thread=True)

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
        self.run_worker(self._load, thread=True)

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
                yield Button("Открыть страницу токена", id="tokenpage")
                yield Button("Выключить Jira", id="off")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "tokenpage":
            subprocess.Popen(["open", "https://id.atlassian.com/manage-profile/security/api-tokens"])
            self.app.notify("открыл страницу создания токена в браузере")
            return
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


class CCApp(App):
    CSS = """
    Tree { width: 44%; border-right: solid $accent; }
    #detail { padding: 1 2; }
    """
    BINDINGS = [
        Binding("a", "add_project", "+Project"),
        Binding("R", "reviewers", "Reviewers"),
        Binding("j", "project_jira", "Jira/settings"),
        Binding("e", "new_epic", "+Epic"),
        Binding("n", "new_task", "+Task"),
        Binding("o", "open", "Chat"),
        Binding("v", "view_chat", "View"),
        Binding("c", "cursor", "Cursor"),
        Binding("d", "diff", "Diff"),
        Binding("m", "mr", "MR dry"),
        Binding("M", "mr_real", "MR!"),
        Binding("g", "mrs", "MR links"),
        Binding("x", "cleanup", "Cleanup"),
        Binding("r", "refresh", "Refresh"),
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
        self.build_tree()
        self.set_interval(5.0, self.refresh_glyphs)
        self.set_interval(90.0, self.kick_sync)

    def kick_sync(self):
        self.run_worker(self._sync_mrs, thread=True, exclusive=True, group="mrsync")

    def _sync_mrs(self):
        s = cc.load_state()
        tids = [tid for tid, t in s["tasks"].items() if t.get("mrs") and not t.get("merged")]
        for tid in tids:
            subprocess.run([sys.executable, ENGINE, "task", "mrs", tid], capture_output=True)
        if tids:
            self.call_from_thread(self.build_tree)

    def state(self):
        return cc.load_state()

    def build_tree(self):
        tree = self.query_one("#tree", Tree)
        tree.clear()
        s = self.state()
        tree.root.expand()
        for pname, p in s["projects"].items():
            pn = tree.root.add("%s  [%d repo]" % (pname, len(p["repos"])),
                               data={"type": "project", "id": pname}, expand=True)
            has_epic = False
            for ekey, e in s["epics"].items():
                if e["project"] != pname:
                    continue
                has_epic = True
                en = pn.add("# %s  %s" % (ekey, e.get("summary", "")),
                            data={"type": "epic", "id": ekey}, expand=True)
                for tid, t in s["tasks"].items():
                    if t["epic"] != ekey:
                        continue
                    en.add_leaf("%s %s" % (GLYPH.get(cc.task_status(t), "?"), t["title"]),
                                data={"type": "task", "id": tid})
            if not has_epic:
                pn.add_leaf("(no epics — press 'e' to add one)", data=None)

    def current(self):
        node = self.query_one("#tree", Tree).cursor_node
        return node.data if node and node.data else None

    def on_tree_node_highlighted(self, event):
        self.show_detail(event.node.data if event.node else None)

    def show_detail(self, data):
        d = self.query_one("#detail", Static)
        if not data:
            d.update("")
            return
        s = self.state()
        if data["type"] == "task" and data["id"] in s["tasks"]:
            t = s["tasks"][data["id"]]
            L = ["[b]%s[/b]   status=%s" % (t["title"], cc.task_status(t)),
                 "epic: %s    branch: %s" % (t["epic"], t["branch"]), "", "repos -> target:"]
            for r in t["repos"]:
                L.append("  %s -> %s" % (r, t["base"][r]))
                if t["mrs"].get(r):
                    L.append("     MR: %s" % t["mrs"][r])
            if t.get("merged"):
                L += ["", "[bold green]✓ все MR влиты — x = очистить worktrees[/bold green]"]
            L += ["", "[b]changes:[/b]"]
            for r in t["repos"]:
                st = cc._changed(t["worktrees"][r])
                L.append("  [%s] %s" % (r, st.replace("\n", "; ") if st else "(none)"))
            L += ["", "[dim]o=chat v=view c=cursor d=diff m=dry M=create g=links x=cleanup[/dim]"]
            d.update("\n".join(L))
        elif data["type"] == "epic":
            e = s["epics"][data["id"]]
            mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
            L = ["[b]epic %s[/b]  %s   [%s]" % (data["id"], e.get("summary", ""), mode), ""]
            if mode == "epic_branch":
                L += ["tasks -> branch '%s' -> MR to master/main" % data["id"]]
            else:
                L += ["routing:"]
                for r, b in e.get("targets", {}).items():
                    L.append("  %s -> %s" % (r, b))
            L += ["", "[dim]n=new task   m/M=epic MR (dry/real)[/dim]"]
            d.update("\n".join(L))
        elif data["type"] == "project":
            p = s["projects"][data["id"]]
            j = p.get("jira", {})
            jira_line = ("on — %s @ %s" % (j.get("project_key", "?"), j.get("site", "?"))) if j.get("token") else "off"
            L = ["[b]project %s[/b]  (%s, %d repos)" % (data["id"], p["kind"], len(p["repos"])),
                 "assignee: %s    Jira: %s" % (p.get("default_assignee") or "-", jira_line),
                 "", "repos (reviewer):"]
            for r, ri in p["repos"].items():
                L.append("  %-22s %s" % (r, ri.get("reviewer") or "— (R чтобы задать)"))
            L += ["", "[dim]e=новый эпик   R=ревьюеры   j=Jira/настройки[/dim]"]
            d.update("\n".join(L))

    def refresh_glyphs(self):
        s = self.state()

        def walk(node):
            for ch in node.children:
                dd = ch.data
                if dd and dd.get("type") == "task" and dd["id"] in s["tasks"]:
                    t = s["tasks"][dd["id"]]
                    ch.set_label("%s %s" % (GLYPH.get(cc.task_status(t), "?"), t["title"]))
                walk(ch)
        walk(self.query_one("#tree", Tree).root)

    def action_refresh(self):
        self.build_tree()
        self.notify("refreshed")

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
        return None

    # ---- create epic ----
    def action_add_project(self):
        self.push_screen(AddProjectScreen(), self._project_added)

    def action_reviewers(self):
        proj = self._current_project()
        if not proj:
            self.notify("select a project node first", severity="error"); return
        self.push_screen(ReviewersScreen(proj))

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
        epic = self._current_epic()
        if not epic:
            self.notify("select an epic (or its task) first", severity="error")
            return
        self.push_screen(NewTaskScreen(epic), self._task_created)

    def _task_created(self, res):
        if not res:
            return
        args = [sys.executable, ENGINE, "task", "add", res["epic"], res["title"], "--prompt", res["prompt"]]
        self.notify("launching task '%s' ..." % res["title"])
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            self.notify("task failed: %s" % (r.stderr.strip()[:120]), severity="error")
        self.build_tree()

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
        return cwd, ("claude --resume %s" % sid if sid else "claude")

    def action_view_chat(self):
        tid = self._cur_task()
        if not tid:
            return
        self.push_screen(ChatScreen(tid))

    def action_open(self):
        tid = self._cur_task()
        if not tid:
            return
        t = self.state()["tasks"][tid]
        cwd, chat = self._chat_cmd(t)
        if not os.path.isdir(cwd):
            self.notify("worktree missing — recreate the task", severity="error"); return
        where = open_cmux("%s chat" % tid, cwd, chat)
        self.notify("чат задачи открыт (%s) — печатай там, отвечай на вопросы агента" % where)

    def action_cursor(self):
        tid = self._cur_task()
        if not tid:
            return
        subprocess.run([sys.executable, ENGINE, "task", "open", tid], capture_output=True)
        ws = Path.home() / ".cc" / "workspaces" / ("%s.code-workspace" % tid)
        if shutil.which("cursor") and ws.exists():
            subprocess.Popen(["cursor", str(ws)])
            self.notify("Cursor opened for %s" % tid)
        else:
            self.notify("cursor CLI or workspace missing", severity="error")

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
        tid = self._cur_task()
        if not tid:
            return
        self.push_screen(OutputScreen("MR links: %s" % tid,
                         [sys.executable, "-u", ENGINE, "task", "mrs", tid]),
                         lambda _: self.build_tree())

    def action_cleanup(self):
        tid = self._cur_task()
        if not tid:
            return
        self.push_screen(OutputScreen("cleanup: %s" % tid,
                         [sys.executable, "-u", ENGINE, "task", "done", tid]),
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
        self.push_screen(OutputScreen(title, argv), lambda _: self.build_tree())


if __name__ == "__main__":
    CCApp().run()
