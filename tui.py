#!/usr/bin/env python3
"""cc TUI — terminal interface over the cc engine (Phase 3, Textual)."""
import json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cc
from dataclasses import replace
import tui_i18n
from tui_i18n import tr

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Tree, Static, Input, Button, Label, TextArea, Select
from textual.binding import Binding
from rich.markup import escape
import threading

ENGINE = str(Path(__file__).parent / "cc.py")
GLYPH = {"running": "🔵", "review": "🟡", "mr": "🟣", "merged": "✅", "idle": "⚪", "done": "✅"}
LEGEND = ("🟡 needs review   🔵 agent running   🟣 MR open   ✅ merged   ⚪ idle   "
          "💬 new reply   ❓ needs your answer   ·   # group   ⚠️ lost tasks (U)   ·   "
          "o chat · n task · P panel · G move to group · L timeline")

_AUDIT_VERB = {"task.add": "created", "task.merge": "merged", "epic.mr": "epic MR→master",
               "jira.transition": "Jira", "repo.set": "repo set", "state.restore": "state restored",
               "state.recover": "tasks recovered", "group.combine": "group combined",
               "task.regroup": "task moved", "task.land": "task landed",
               "task.memory": "task memory updated", "ops.start": "operation started"}

_AUDIT_FIELD = {"repo": "repo", "base": "target", "mr": "MR", "url": "URL", "to": "to",
                "key": "key", "branch": "branch", "default_branch": "default branch",
                "remote": "remote", "title": "title", "repos": "repos", "skipped": "skipped",
                "count": "count", "reason": "reason", "detail": "detail", "from": "from",
                "included": "included", "op": "operation", "kind": "kind"}


def _timeline_lines(output):
    """`_timeline_lines(output)` is localized timeline text from the engine's JSON output."""
    records = json.loads(output)
    if not records:
        return [tr("(timeline is empty — no actions recorded yet)")]
    lines = []
    for record in records:
        timestamp = time.strftime("%H:%M %d.%m", time.localtime(record.get("ts", 0)))
        action = record.get("action", "?")
        verb = tr(_AUDIT_VERB[action]) if action in _AUDIT_VERB else action
        who = record.get("task") or record.get("epic") or record.get("project") or ""
        fields = []
        for key, label in _AUDIT_FIELD.items():
            if key in record:
                value = record[key]
                if isinstance(value, list):
                    value = ",".join(map(str, value))
                fields.append("%s=%s" % (tr(label), value))
        lines.append("%s  %s  %s  %s" % (timestamp, verb, who, "  ".join(fields)))
    return lines


def _recovery_lines(output):
    """`_recovery_lines(output)` is localized recovery results from the engine's JSON output."""
    records = json.loads(output)
    if not records:
        return [tr("No lost tasks on disk to recover ✓")]
    return [tr("Recovered %d tasks to the board:") % len(records),
            *[tr("  %s   (epic %s, branch %s)") % tuple(record) for record in records], "",
            tr("`cc task mrs <tid>` loads MRs; task names use the folder slug.")]

def _audit_brief(r):
    """One compact human line for an audit record (used in the task detail's timeline block)."""
    verb = tr(_AUDIT_VERB[r["action"]]) if r.get("action") in _AUDIT_VERB else r.get("action", "?")
    bits = []
    if r.get("repo"):
        bits.append(r["repo"])
    if r.get("mr"):
        bits.append("!%s" % r["mr"])
    if r.get("base"):
        bits.append("→ %s" % r["base"])
    if r.get("to"):
        bits.append(r["to"])
    if r.get("default_branch"):
        bits.append("default=%s" % r["default_branch"])
    if r.get("detail") and r.get("action", "").startswith("state."):
        bits.append(r["detail"])
    return ("%s  %s" % (verb, "  ".join(bits))).rstrip()


def _parse_mr_plan(text):
    """Parse `cc task mr --dry-run` / `cc epic mr --dry-run` output into [(repo, source, target, prod)].
    `prod` = the MR lands on master/main (epic->master is always a release). Used to show the user
    EXACTLY where each MR will go before any is created."""
    rows = []
    for ln in text.splitlines():
        m = re.match(r"\[([^\]]+)\] DRY-RUN epic MR: (\S+) -> (\S+)", ln)   # epic->master release MR
        if m:
            rows.append((m.group(1), m.group(2), m.group(3), True))
            continue
        m = re.match(r"\[([^\]]+)\] DRY-RUN: (\S+) -> (\S+)", ln)           # task MR (-> base/epic branch)
        if m:
            tgt = m.group(3)
            rows.append((m.group(1), m.group(2), tgt, tgt in ("master", "main")))
    return rows


def _group_options(s, tid):
    """(options, cur_key) for moving a task to another group: non-loose/non-archived epics in the
    task's project, excluding its current group, plus an ungroup option (unless the task is already
    loose). Option value is the epic key, or the project name for ungroup."""
    t = s["tasks"][tid]
    cur = t["epic"]
    proj_name = s["epics"][cur]["project"]
    opts = []
    for k, e in s["epics"].items():
        if e.get("project") == proj_name and not e.get("loose") and not e.get("archived") and k != cur:
            opts.append(("%s — %s" % (k, (e.get("summary") or "")[:40]), k))
    if not s["epics"].get(cur, {}).get("loose"):
        opts.append((tr("(no group — directly in project)"), proj_name))
    return opts, cur


def _group_panel_rows(s, ekey):
    """Read-only data for the group control panel: the epic + one row per task, each with per-repo
    MR/merge state. Returns (epic_dict, [ {tid,title,status,merged, repos:[(repo, mr_url|None, base)]} ]).
    Honest about what we track: MR url + task-level merged flag; deploy/stage state is NOT here yet."""
    e = s["epics"].get(ekey, {})
    rows = []
    for tid, t in s["tasks"].items():
        if t.get("epic") != ekey:
            continue
        mrs = t.get("mrs") or {}
        base = t.get("base") or {}
        repos = [(r, mrs.get(r), base.get(r, "?")) for r in t.get("repos", [])]
        rows.append({"tid": tid, "title": t.get("title", tid), "status": fast_status(t),
                     "merged": bool(t.get("merged")), "repos": repos})
    rows.sort(key=lambda r: (r["merged"], r["tid"]))   # un-merged first (active on top), stable by tid
    return e, rows


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


def _ops_exit(o):
    """The REAL exit code the launcher wrapper recorded for the ops agent (int), or None if the
    process died without writing one (a crash). This is the FACT cc trusts, not the agent's prose."""
    p = o.get("exit")
    if not p:
        return None
    try:
        return int(open(p).read().strip())
    except Exception:
        return None


def _ops_status(o):
    """Status from FACTS, not the agent's narration: running while alive; then the agent's REAL exit
    code (0 -> done, nonzero or crashed -> failed); and for a deploy, the CI pipeline FACT (②b) — a
    0-exit agent still shows ❌ if the pipeline it triggered FAILED. [cc-needs-input] -> needs_input.
    A run can NEVER show ✓ just because the agent SAID so."""
    if o.get("pid") and cc.pid_alive(o["pid"]):
        return "running"
    if cc.task_needs_input({"log": o.get("log")}):
        return "needs_input"
    if "exit" not in o:
        return "done"                  # legacy ops record (pre-facts layer) — don't false-alarm
    if _ops_exit(o) != 0:
        return "failed"
    ci = cc._ci_facts(o)               # agent exited 0 — but the deploy's truth is its CI pipeline
    if ci and ci.get("checked") and any(p.get("status") == "failed" for p in ci.get("pipelines", [])):
        return "failed"
    return "done"


OPS_GLYPH = {"running": "🔵", "needs_input": "❓", "done": "✅", "failed": "❌"}


def status_label(status):
    """`status_label(status)` is a localized display label, preserving unknown status values."""
    labels = {"running": tr("Running"), "review": tr("Needs review"), "mr": tr("MR open"),
              "merged": tr("Merged"), "idle": tr("Idle"), "done": tr("Done"),
              "failed": tr("Failed"), "needs_input": tr("Needs an answer"),
              "open": tr("Open"), "opened": tr("Open"), "closed": tr("Closed"), "locked": tr("Locked")}
    return labels.get(status, status)


def localize_bindings(node):
    """`localize_bindings(node)` is an in-place translation of NODE's binding labels."""
    for source in node.BINDINGS:
        # Textual has no public Screen API for changing binding descriptions.
        bindings = node._bindings.key_to_bindings.get(source.key, [])
        node._bindings.key_to_bindings[source.key] = [
            replace(binding, description=tr(source.description))
            if binding.action == source.action else binding for binding in bindings
        ]


class TranslatedModalScreen(ModalScreen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize_bindings(self)


class LanguageScreen(TranslatedModalScreen):
    CSS = """
    LanguageScreen { align: center middle; }
    #language-dialog { width: 58; max-width: 95%; height: auto;
                       padding: 1 2; border: round $accent; background: $surface; }
    #language-dialog Label, #language-dialog Select { margin-bottom: 1; }
    #language-dialog Horizontal { height: auto; }
    #language-dialog Button { margin-right: 2; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="language-dialog"):
            yield Label(tr("Display language / Язык интерфейса"))
            yield Select([(name, code) for code, name in tui_i18n.LANGUAGES.items()],
                         value=tui_i18n.get_language(), allow_blank=False, id="language-choice")
            with Horizontal():
                yield Button(tr("Apply"), id="apply-language", variant="primary")
                yield Button(tr("Cancel"), id="cancel-language")

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "language-choice" and event.value in tui_i18n.LANGUAGES:
            self.query_one("#apply-language", Button).label = tr("Apply", language=event.value)
            self.query_one("#cancel-language", Button).label = tr("Cancel", language=event.value)

    def on_button_pressed(self, event):
        if event.button.id == "apply-language":
            self.dismiss(self.query_one("#language-choice", Select).value)
        else:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class NewEpicScreen(TranslatedModalScreen):
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
                yield Label(tr("[b]Jira epic[/b] for '%s'") % self.project)
                with VerticalScroll(id="body"):
                    yield Label(tr("[dim]Use an existing epic (newest first; search by name):[/dim]"))
                    yield Input(placeholder=tr("search by name — Enter (all project epics)"), id="esearch")
                    yield Select([(tr("(loading project epics…)"), "_")], id="epick", allow_blank=True)
                    yield Button(tr("Use selected"), variant="success", id="use")
                    yield Static(tr("──────────  or  ──────────"), classes="sep")
                    yield Label(tr("[dim]Create a new Jira epic:[/dim]"))
                    yield Input(placeholder=tr("new epic name"), id="enew")
                    yield Button(tr("Create in Jira"), variant="primary", id="createnew")
                with Horizontal(id="row"):
                    yield Button(tr("Cancel"), id="cancel")
            else:
                yield Label(tr("[b]New epic[/b] in project '%s'") % self.project)
                with VerticalScroll(id="body"):
                    yield Input(placeholder=tr("key (e.g. IK-8631)"), id="key")
                    yield Input(placeholder=tr("summary"), id="summary")
                    yield Input(placeholder=tr("targets repo=branch,… (empty = epic gets its own branch)"), id="targets")
                    yield Input(placeholder=tr("comma-separated repos; empty = ALL"), id="erepos")
                with Horizontal(id="row"):
                    yield Button(tr("Cancel"), id="cancel")
                    yield Button(tr("Create"), variant="success", id="ok")

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
            sel.set_options(opts or [(tr("(none — search or create a new one below)"), "_")])
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
                self.app.notify(tr("key is required"), severity="error"); return
            self.dismiss({"key": key,
                          "summary": self.query_one("#summary", Input).value.strip(),
                          "targets": self.query_one("#targets", Input).value.strip(),
                          "repos": self.query_one("#erepos", Input).value.strip()})
            return
        if event.button.id == "use":
            val = self.query_one("#epick", Select).value
            if val in (None, "_", Select.BLANK):
                self.app.notify(tr("select an epic from the list"), severity="error"); return
            summary = next((e["summary"] for e in self._epics if e["key"] == val), "")
            self.dismiss({"action": "pick", "key": str(val), "summary": summary}); return
        if event.button.id == "createnew":
            summary = self.query_one("#enew", Input).value.strip()
            if not summary:
                self.app.notify(tr("enter an epic name"), severity="error"); return
            self.dismiss({"action": "create", "summary": summary}); return


class NewTaskScreen(TranslatedModalScreen):
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

    def __init__(self, epic, jira_on=False, project=None, preset_key=None, preset_summary="", loose=False):
        super().__init__()
        self.epic = epic
        self.jira_on = jira_on
        self.project = project
        self.loose = loose
        self.preset_key = preset_key
        self.preset_summary = preset_summary
        self._children = []
        self._jira_key = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            if self.loose:
                yield Label(tr("[b]New task[/b] in project '%s' (no epic)") % self.project)
                yield Label(tr("[dim]MR targets master/main; all project repos are included, with MRs only for changed repos[/dim]"))
            else:
                yield Label(tr("[b]New task[/b] under epic '%s'") % self.epic)
                yield Label(tr("[dim]all project repos are included; MRs only for repos changed by the agent[/dim]"))
            with VerticalScroll(id="body"):
                if self.jira_on:
                    with Vertical(id="jira"):
                        if self.loose:
                            yield Label(tr("[dim]from Jira (optional): any project issue → link to the board as-is (no move in Jira)[/dim]"))
                            yield Input(placeholder=tr("search project issues (Enter)"), id="tsearch")
                            yield Select([(tr("(loading project issues…)"), "_")], id="tpick", allow_blank=True)
                        else:
                            yield Label(tr("[dim]from Jira (optional): issue in this epic OR without an epic (move under epic) → link and prefill[/dim]"))
                            yield Input(placeholder=tr("search epic issues (Enter)"), id="tsearch")
                            yield Select([(tr("(loading epic issues…)"), "_")], id="tpick", allow_blank=True)
                        yield Button(tr("Fill from Jira"), id="seed")
                yield Label(tr("title (optional):"))
                yield Input(placeholder=tr("empty = cc generates a title from the prompt"), id="title")
                yield Label(tr("prompt (what the agent should do):"))
                yield TextArea(id="prompt")
            with Horizontal(id="row"):
                yield Button(tr("Cancel"), id="cancel")
                yield Button(tr("Launch"), variant="success", id="ok")

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
            self.app.notify(tr("from %s — edit the prompt and Launch") % key)
        self.app.call_from_thread(fill)

    def _load_children(self, query):
        try:
            cfg = cc.load_state()["projects"][self.project].get("jira", {})
            if self.loose:
                # epic-less task: pick ANY existing project ticket (linked as-is, not reparented)
                self._children = cc.jira_project_tasks(cfg, query)
            else:
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
                if self.loose:
                    tag = (tr("  · epic %s") % c["parent"]) if c.get("parent") else tr("  · no epic")
                else:
                    tag = tr("  · NO EPIC (will move)") if c.get("orphan") else ""
                opts.append(("%s — %s [%s]%s" % (c["key"], c["summary"][:38], c["status"], tag), c["key"]))
            sel.set_options(opts or [(tr("(no issues found)"), "_")])
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
            self.app.notify(tr("filled from %s — edit the prompt and Launch") % key)
        self.app.call_from_thread(fill)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "seed":
            val = self.query_one("#tpick", Select).value
            if val in (None, "_", Select.BLANK):
                self.app.notify(tr("select an issue from the list"), severity="error"); return
            self._jira_key = str(val)
            self.app._daemon(lambda: self._seed(str(val))); return
        title = self.query_one("#title", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            self.app.notify(tr("a prompt is required (cc generates a title if empty)"), severity="error"); return
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



class AddProjectScreen(TranslatedModalScreen):
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
            yield Label(tr("Add project — folder with repos (single or multi-repo)"))
            yield Input(placeholder=tr("project folder path"), id="ppath")
            yield Input(placeholder=tr("name (optional; defaults to folder name)"), id="pname")
            with Horizontal(id="row"):
                yield Button(tr("Browse…"), id="browse")
                yield Button(tr("Add"), variant="success", id="ok")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "browse":
            r = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt %s)' % json.dumps(tr("Choose project folder"), ensure_ascii=False)],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                self.query_one("#ppath", Input).value = r.stdout.strip().rstrip("/")
            return
        path = self.query_one("#ppath", Input).value.strip()
        if not path:
            self.app.notify(tr("specify a folder path"), severity="error"); return
        self.dismiss({"path": path, "name": self.query_one("#pname", Input).value.strip()})


class OutputScreen(TranslatedModalScreen):
    CSS = """
    OutputScreen { align: center middle; }
    #obox { width: 90%; height: 85%; border: thick $accent; background: $surface; }
    #ocontent { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"),
                Binding("o", "open_links", "Open links")]

    def __init__(self, title, argv, formatter=None):
        super().__init__()
        self._title = title
        self._argv = argv
        self._formatter = formatter
        self._lines = []

    def action_open_links(self):
        urls = []
        for ln in self._lines:
            urls += re.findall(r"https?://[^\s)\]\"']+", ln)
        urls = list(dict.fromkeys(urls))
        if not urls:
            self.app.notify(tr("no links in output")); return
        for u in urls:
            subprocess.Popen(["open", u])
        self.app.notify(tr("opened %d links in browser") % len(urls))

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="obox"):
            yield Static("⏳ %s …" % self._title, id="ocontent")
        yield Footer()

    def on_mount(self):
        self._lines = [tr("⏳ %s — running…") % self._title]
        try:
            self.query_one("#obox", VerticalScroll).border_title = tr("%s  ·  esc = close") % self._title
        except Exception:
            pass
        self.app._bg("output:%d" % id(self), self._run)

    def _run(self):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"   # never block on a credential prompt
        try:
            proc = subprocess.Popen(self._argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=env)
            output = []
            for line in proc.stdout:
                if self._formatter:
                    output.append(line)
                else:
                    self.app.call_from_thread(self._append, line.rstrip("\n"))
            proc.wait()
            if self._formatter:
                lines = "".join(output).splitlines()
                if proc.returncode == 0:
                    try:
                        lines = self._formatter("".join(output))
                    except (ValueError, TypeError, KeyError):
                        pass
                for line in lines:
                    self.app.call_from_thread(self._append, line)
            tail = tr("✓ done (exit 0)") if proc.returncode == 0 else tr("✗ ERROR (exit %d)") % proc.returncode
        except Exception as ex:
            tail = tr("✗ Launch ERROR: %s") % ex
        self.app.call_from_thread(self._append, "")
        self.app.call_from_thread(self._append, tail)

    def _append(self, line):
        if self._lines == [tr("⏳ %s — running…") % self._title]:
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


class MRConfirmScreen(TranslatedModalScreen):
    """Loud confirmation BEFORE any MR is created: shows each repo's source -> target, flags the
    prod-bound ones (master/main), and refuses to proceed without an explicit y/Enter. Computes the
    plan off-thread by running the dry-run first (creates nothing)."""
    CSS = """
    MRConfirmScreen { align: center middle; }
    #mrbox { width: 80%; height: auto; max-height: 80%; border: thick $warning; background: $surface; padding: 1 2; }
    #mrc { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("q", "cancel", "Cancel"),
                Binding("y", "confirm", "Create"), Binding("enter", "confirm", "Create")]

    def __init__(self, title, dry_argv):
        super().__init__()
        self._title = title
        self._dry = dry_argv
        self._rows = None          # None = still computing; [] = nothing to create

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="mrbox"):
            yield Static(tr("⏳ checking MR targets …"), id="mrc")
        yield Footer()

    def on_mount(self):
        try:
            self.query_one("#mrbox", VerticalScroll).border_title = self._title
        except Exception:
            pass
        self.app._bg("mrplan:%d" % id(self), self._compute)

    def _compute(self):
        try:
            out = subprocess.run(self._dry, capture_output=True, text=True, timeout=60)
            text = (out.stdout or "") + "\n" + (out.stderr or "")
        except Exception as ex:
            text = "ERR %s" % ex
        self.app.call_from_thread(self._show_plan, _parse_mr_plan(text))

    def _show_plan(self, rows):   # NB: not `_render` — that name collides with textual's Widget._render
        self._rows = rows
        from rich.text import Text
        t = Text()
        if not rows:
            t.append(tr("Nothing to create — no changes in any repo.\n\n"), style="dim")
            t.append(tr("esc — close"), style="bold")
            self.query_one("#mrc", Static).update(t); return
        prod = [r for r in rows if r[3]]
        t.append(tr("CREATE %d MRs — check their targets:\n\n") % len(rows), style="bold")
        for repo, src, tgt, is_prod in rows:
            if is_prod:
                t.append("  %s\n" % repo, style="bold")
                t.append("     %s  →  %s   ⚠ PROD (master/main)\n" % (src, tgt), style="bold red")
            else:
                t.append("  %s\n" % repo)
                t.append("     %s  →  %s\n" % (src, tgt), style="green")
        t.append("\n")
        if prod:
            t.append(tr("⚠ %d MRs target master/main — this is a production release.\n") % len(prod), style="bold yellow")
        t.append(tr("\ny / Enter — create"), style="bold")
        t.append(tr("        esc / q — cancel"), style="dim")
        self.query_one("#mrc", Static).update(t)

    def action_confirm(self):
        if self._rows:          # only confirm once the plan is loaded AND non-empty
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self):
        self.dismiss(False)


class ConfirmScreen(TranslatedModalScreen):
    """Generic yes/no confirm -> dismiss(True/False). For low-stakes actions (e.g. merging task MRs
    into an epic branch — not prod). The heavy per-repo gate (MRConfirmScreen) is for prod MR creation."""
    CSS = """
    ConfirmScreen { align: center middle; }
    #cdlg { width: 72; max-width: 92%; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #cdlg Label { margin-bottom: 1; }
    #cdlg Button { margin: 0 2 0 0; min-width: 14; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("q", "cancel", "Cancel"),
                Binding("y", "ok", "Yes"), Binding("enter", "ok", "Yes")]

    def __init__(self, title, message, ok_label="Yes"):
        super().__init__()
        self._title = title
        self._message = message
        self._ok_label = tr(ok_label)

    def compose(self) -> ComposeResult:
        with Vertical(id="cdlg"):
            yield Label("[b]%s[/b]" % self._title)
            yield Label("[dim]%s[/dim]" % self._message)
            with Horizontal():
                yield Button(self._ok_label, id="ok", variant="primary")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "ok")

    def action_ok(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class ReviewersScreen(TranslatedModalScreen):
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
            yield Static(tr("[b]Reviewer per repo[/b] — select a GitLab member. esc = close\n"))
            s = cc.load_state()
            for r in s["projects"][self.project]["repos"]:
                with Horizontal(classes="rrow"):
                    yield Label(r)
                    yield Select([(tr("(loading…)"), "_")], id="sel__" + r, allow_blank=True)
        yield Footer()

    def on_mount(self):
        try:
            self.query_one("#rbox", VerticalScroll).border_title = tr("Reviewers — %s") % self.project
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
            sel.set_options(opts or [(tr("(no members)"), "_")])
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
        self.app.notify(tr("%s -> reviewer %s") % (repo, val))

    def action_close(self):
        self.dismiss(None)


class ProjectJiraScreen(TranslatedModalScreen):
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
            yield Label(tr("Jira for project '%s'") % self.project)
            yield Label(tr("[dim]need a token? click the link (or button) to open it in a browser:[/dim]"))
            yield Static("[@click=screen.open_token][u]https://id.atlassian.com/manage-profile/security/api-tokens[/u][/]")
            yield Input(value=j.get("site", ""), placeholder=tr("site (you.atlassian.net)"), id="jsite")
            yield Input(value=j.get("email", ""), placeholder=tr("email"), id="jemail")
            yield Input(placeholder=(tr("token: ••• (enter to change)") if j.get("token") else tr("API token (paste here)")),
                        id="jtoken", password=True)
            yield Input(value=j.get("project_key", ""), placeholder=tr("project key (e.g. IK)"), id="jkey")
            with Horizontal(id="row"):
                yield Button(tr("Save"), variant="success", id="save")
                yield Button(tr("Disconnect Jira"), id="off")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None); return
        if event.button.id == "off":
            subprocess.run([sys.executable, ENGINE, "project", "jira", self.project, "--off"], capture_output=True)
            self.app.notify(tr("Jira disabled for %s") % self.project); self.dismiss(True); return
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
            self.app.notify(tr("Jira connected ✓ (%s)") % (out.split("ok —")[1].strip()[:60]))
        elif "WARN" in out:
            self.app.notify(tr("saved, but authentication failed — check token/email"), severity="error")
        else:
            self.app.notify(tr("Jira saved for %s") % self.project)
        self.dismiss(True)

    def action_close(self):
        self.dismiss(None)


class EpicManageScreen(TranslatedModalScreen):
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
            yield Label(tr("[b]Epic %s[/b]") % self.key)
            yield Label(tr("[dim]“Done and remove from board”:\n  • move the epic and ALL its issues to Done in Jira (skip those already Done)\n  • remove local task worktrees/branches/folders\n  • remove the epic from cc (history stays in Jira + git; remote MRs/branches remain)[/dim]"))
            with Horizontal():
                yield Button(tr("Done and remove from board"), id="done", variant="primary")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self):
        self.dismiss(None)


class TaskManageScreen(TranslatedModalScreen):
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
            yield Label(tr("[b]Task %s[/b]") % self.tid)
            yield Label(tr("[dim]“Done and remove from board” = Jira issue -> Done + remove local worktrees + remove from cc (remote MR/branch remain; refuses uncommitted changes).\nAbort = discard: close MRs + delete remote branches + remove worktrees + remove from cc.[/dim]"))
            with Horizontal():
                yield Button(tr("Done and remove from board"), id="done", variant="primary")
                yield Button(tr("Abort (discard)"), id="abort", variant="error")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self):
        self.dismiss(None)


class MoveToGroupScreen(TranslatedModalScreen):
    """Pick a group to move a task into (task-based regrouping). Returns the chosen epic key, the
    project name for ungroup, or None on cancel."""
    CSS = """
    MoveToGroupScreen { align: center middle; }
    #dlg { width: 78; max-width: 92%; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #dlg Label { margin-bottom: 1; }
    #dlg Select { margin-bottom: 1; width: 1fr; }
    #dlg Button { margin: 0 2 0 0; min-width: 14; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, tid, options, cur):
        super().__init__()
        self.tid = tid
        self._options = options
        self.cur = cur

    def compose(self):
        with Vertical(id="dlg"):
            yield Label(tr("[b]Move task %s[/b]   (currently: %s)") % (self.tid, self.cur))
            yield Label(tr("[dim]Changes the group and recalculates the MR target. Branch and worktree stay intact. Only for tasks without MRs.[/dim]"))
            yield Select(self._options, id="grp", prompt=tr("select a group"))
            with Horizontal():
                yield Button(tr("Move"), id="ok", variant="primary")
                yield Button(tr("Cancel"), id="cancel")

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None); return
        val = self.query_one("#grp", Select).value
        if val in (None, Select.BLANK):
            self.app.notify(tr("select a group"), severity="error"); return
        self.dismiss(str(val))

    def action_cancel(self):
        self.dismiss(None)


class GroupPanelScreen(TranslatedModalScreen):
    """Read-only control panel for a GROUP: every task + per-repo MR/merge state at a glance.
    Action buttons (Test / Stage / Release) come next; deploy/stage state isn't tracked yet."""
    CSS = """
    GroupPanelScreen { align: center middle; }
    #pbox { width: 88%; height: 85%; border: thick $accent; background: $surface; }
    #pscroll { height: 1fr; }
    #pc { padding: 0 1; }
    #prow { height: auto; align-horizontal: right; padding: 1 1 0 1; }
    #prow Button { margin: 0 0 0 2; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"),
                Binding("o", "open_links", "Open MRs")]

    def __init__(self, ekey, epic, rows):
        super().__init__()
        self.ekey = ekey
        self._epic = epic
        self._rows = rows
        self._urls = []

    def compose(self) -> ComposeResult:
        with Vertical(id="pbox"):
            with VerticalScroll(id="pscroll"):
                yield Static("", id="pc")
            with Horizontal(id="prow"):
                yield Button(tr("Test (agent)"), id="ops_test", variant="success")
                yield Button(tr("Stage (agent)"), id="ops_stage", variant="success")
                if not self._epic.get("loose"):
                    yield Button(tr("Merge tasks into epic"), id="merge_tasks")
                    yield Button(tr("Release: epic MR→master"), id="release_mr", variant="warning")
                yield Button(tr("Close"), id="close")
        yield Footer()

    def on_mount(self):
        mode = self._epic.get("mode") or ("targets" if self._epic.get("targets") else "epic_branch")
        try:
            self.query_one("#pbox", Vertical).border_title = tr("Group %s · %s · esc=close") % (self.ekey, mode)
        except Exception:
            pass
        from rich.text import Text
        t = Text()
        t.append(tr("Group %s") % self.ekey, style="bold")
        if self._epic.get("summary"):
            t.append("  %s" % self._epic["summary"])
        t.append("\n\n")
        if not self._rows:
            t.append(tr("no tasks in group (n — create)\n"), style="dim")
        merged_n = 0
        for r in self._rows:
            if r["merged"]:
                merged_n += 1
            t.append("%s %s" % (GLYPH.get(r["status"], "?"), r["title"]), style="bold")
            t.append("   [%s]\n" % r["tid"], style="dim")
            for repo, mr, base in r["repos"]:
                if mr:
                    self._urls.append(mr)
                    state = tr("merged ✓") if r["merged"] else tr("MR open")
                    t.append("    %-16s → %-22s %s\n" % (repo, base, state),
                             style=("magenta" if r["merged"] else "green"))
                else:
                    t.append("    %-16s → %-22s —\n" % (repo, base), style="dim")
            t.append("\n")
        if self._rows:
            t.append(tr("total: %d tasks, %d merged\n\n") % (len(self._rows), merged_n), style="bold")
        if self._epic.get("loose"):
            t.append(tr("Ungrouped tasks target master directly — release the task from the board (M on the task).\n"), style="dim")
        else:
            t.append(tr("Release below = MR from the epic branch → master (with confirmation). First merge tasks into the epic branch on the board. Test / Stage is the next step.\n"), style="dim")
        t.append(tr("Stage/prod deployments are not tracked here yet — deployment buttons will add this.\n"), style="dim")
        t.append(tr("o — open MR links (%d) in browser") % len(self._urls), style="dim")
        self.query_one("#pc", Static).update(t)

    def on_button_pressed(self, event):
        bid = event.button.id
        if bid in ("release_mr", "merge_tasks"):
            self.dismiss({"action": bid})
        elif bid == "ops_test":
            self.dismiss({"action": "ops", "kind": "test"})
        elif bid == "ops_stage":
            self.dismiss({"action": "ops", "kind": "stage"})
        else:
            self.dismiss(None)

    def action_open_links(self):
        urls = list(dict.fromkeys(self._urls))
        if not urls:
            self.app.notify(tr("no MR links in group")); return
        for u in urls:
            subprocess.Popen(["open", u])
        self.app.notify(tr("opened %d MRs in browser") % len(urls))

    def action_close(self):
        self.dismiss(None)


class CCApp(App):
    CSS = """
    Tree { width: 44%; border-right: solid $accent; }
    #detail { padding: 1 2; }
    #legend { height: auto; color: $text-muted; background: $panel; padding: 0 1; border-top: solid $accent; }
    """
    BINDINGS = [
        Binding("f2", "language", "Language / Язык"),
        Binding("a", "add_project", "+Project"),
        Binding("R", "reviewers", "Reviewers"),
        Binding("D", "deploys", "Deploys"),
        Binding("j", "project_jira", "Jira/settings"),
        Binding("e", "new_epic", "+Group"),
        Binding("n", "new_task", "+Task"),
        Binding("o", "open", "Chat"),
        Binding("c", "cursor", "Cursor"),
        Binding("M", "mr_real", "MR!"),
        Binding("x", "cleanup", "Remove/manage"),
        Binding("r", "refresh", "Refresh"),
        Binding("U", "recover", "Recover lost"),
        Binding("L", "timeline", "Timeline"),
        Binding("G", "regroup", "→Group"),
        Binding("P", "panel", "Panel"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, *args, language=None, language_file=None, **kwargs):
        self.language_file = language_file
        tui_i18n.set_language(language if language is not None else tui_i18n.load_language(language_file))
        super().__init__(*args, **kwargs)
        localize_bindings(self)

    def action_language(self):
        """`action_language()` is the main screen's display-language selection action."""
        if len(self.screen_stack) == 1:
            self.push_screen(LanguageScreen(), self._change_language)

    def _change_language(self, language):
        """`_change_language(language)` is a persisted change of LANGUAGE and the visible board."""
        if language is None:
            return
        try:
            tui_i18n.save_language(language, self.language_file)
        except OSError as error:
            self.notify(tr("Could not save language: %s") % error, severity="error")
            return
        tui_i18n.set_language(language)
        localize_bindings(self)
        self.title = tr("cc — multi-repo orchestrator")
        self.query_one("#legend", Static).update(tr(LEGEND))
        self.build_tree()
        self.show_detail(self.current())
        self.refresh_bindings()
        self.notify(tr("Display language saved"))

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("cc", id="tree")
            with VerticalScroll():
                yield Static("", id="detail")
        yield Static(tr(LEGEND), id="legend")
        yield Footer()

    def on_mount(self):
        self.title = tr("cc — multi-repo orchestrator")
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
                self.notify(tr("⚠️ %d tasks on disk are missing from the board — press U to recover") % n,
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
        new, ni = {}, {}
        for tid, t in snap["tasks"].items():
            if self._closing:
                return
            running = bool(t.get("pid") and cc.pid_alive(t["pid"]))
            new[tid] = "running" if running else cc.task_status(t)
            ni[tid] = None if running else cc.task_needs_input(t)   # only a finished agent can have left a question
        ops = snap.get("ops") or {}
        new_ops = {oid: _ops_status(o) for oid, o in ops.items()}
        if self._closing:
            return
        changed = (any(snap["tasks"][tid].get("status") != st for tid, st in new.items())
                   or any((snap["tasks"][tid].get("needs_input") or None) != (v or None) for tid, v in ni.items())
                   or any(ops[oid].get("status") != st for oid, st in new_ops.items()))
        if not changed:
            return
        def apply(s):
            for tid, st in new.items():
                if tid in s["tasks"]:
                    s["tasks"][tid]["status"] = st
                    s["tasks"][tid]["needs_input"] = ni.get(tid)
            for oid, st in new_ops.items():
                if oid in s.get("ops", {}):
                    s["ops"][oid]["status"] = st
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

    def _sorted_tasks(self, tasks):
        # primary: status hierarchy (ждут->в работе->на ревью->готово);
        # secondary: most-recently-completed first within a group (recently-closed at the top)
        return sorted(tasks, key=lambda it: (self.TASK_ORDER.get(fast_status(it[1]), 0),
                                             -(it[1].get("merged_at") or 0)))

    def _add_task_leaf(self, parent, tid, t):
        st = fast_status(t)
        if t.get("needs_input") and self._unseen(t):
            badge = "❓ "                                   # agent finished with a question for you
        elif st == "review" and self._unseen(t):
            badge = "💬 "                                   # new unreviewed output
        else:
            badge = ""
        row = "%s%s %s" % (badge, GLYPH.get(st, "?"), t["title"])
        parent.add_leaf("[dim]%s[/dim]" % row if st == "merged" else row,
                        data={"type": "task", "id": tid})

    def _add_ops_leaves(self, parent, ekey, s):
        """Render the group's ops-agent runs (test/stage/deploy) under it, with a live status glyph."""
        for oid, o in (s.get("ops") or {}).items():
            if o.get("epic") != ekey:
                continue
            st = o.get("status") or _ops_status(o)
            parent.add_leaf("%s ⚙ ops: %s" % (OPS_GLYPH.get(st, "⚙"), o.get("kind", "?")),
                            data={"type": "ops", "id": oid})

    def _add_epic_node(self, parent, ekey, e, s, expand=True):
        en = parent.add("# %s  %s" % (ekey, e.get("summary", "")),
                        data={"type": "epic", "id": ekey}, expand=expand)
        tasks = self._sorted_tasks((tid, t) for tid, t in s["tasks"].items() if t["epic"] == ekey)
        for tid, t in tasks:
            self._add_task_leaf(en, tid, t)
        if not tasks:
            en.add_leaf(tr("[dim](no tasks — n to add)[/dim]"), data=None)
        self._add_ops_leaves(en, ekey, s)
        return en

    def _add_loose_tasks(self, pn, pname, s):
        """Epic-less tasks of a project, rendered directly under the project node (below epics)."""
        lk = cc.loose_epic_key(pname)
        loose = self._sorted_tasks((tid, t) for tid, t in s["tasks"].items() if t.get("epic") == lk)
        for tid, t in loose:
            self._add_task_leaf(pn, tid, t)
        self._add_ops_leaves(pn, lk, s)   # loose-group ops runs render under the project too
        return bool(loose)

    @staticmethod
    def _node_key(data):
        """Stable identity for a tree node across rebuilds (for collapse-state + cursor restore)."""
        if not data:
            return None
        t = data.get("type")
        if t in ("project", "epic", "task", "ops"):
            return "%s:%s" % (t, data["id"])
        if t == "orphans":
            return "orphans:"
        if t == "orphan":
            orphan = data["orphan"]
            return "orphan:" + json.dumps([orphan[key] for key in ("project", "epic", "slug")])
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
                         if e["project"] == pname and not e.get("archived") and not e.get("loose")]
                for ekey, e in epics:
                    self._add_epic_node(pn, ekey, e, s, expand=(("epic:" + ekey) not in self._collapsed))
                # epic-less tasks of this project, directly under it (below the epics)
                had_loose = self._add_loose_tasks(pn, pname, s)
                if not epics and not had_loose:
                    pn.add_leaf(tr("(no epics — 'e' to add one, or 'n' for a task without an epic)"), data=None)
            orphans = cc._scan_orphans(s)
            if orphans:
                on = tree.root.add(tr("⚠️  Lost tasks on disk (%d) — U to recover") % len(orphans),
                                   data={"type": "orphans"}, expand=True)
                for o in orphans:
                    on.add_leaf(tr("[yellow]%s / %s  (branch %s, %d repos)[/yellow]")
                                % (o["epic"], o["slug"], o["branch"], len(o["repos"])),
                                data={"type": "orphan", "orphan": o})
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
        try:
            self.show_detail(data)            # a detail-render error must NEVER crash the whole TUI
        except Exception as ex:
            try:
                self.query_one("#detail", Static).update(tr("[red]detail rendering error:[/red] %s") % ex)
            except Exception:
                pass
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
        if data["type"] in ("orphans", "orphan"):
            orphans = [data["orphan"]] if data["type"] == "orphan" else cc._scan_orphans(s)
            lines = [tr("[b]Lost tasks[/b] — %d tasks on disk missing from the board") % len(orphans), ""]
            for orphan in orphans:
                lines.append(tr("Project: %s · Epic: %s · Task: %s") % tuple(
                    escape(str(orphan[key])) for key in ("project", "epic", "slug")))
                lines.append(tr("Branch: %s") % escape(orphan["branch"]))
                lines += ["  %s: %s" % (escape(repo), escape(path)) for repo, path in orphan["repos"].items()]
                lines.append("")
            lines.append(tr("[dim]U — recover lost tasks; existing worktrees and branches are preserved[/dim]"))
            d.update("\n".join(lines))
            return
        if data["type"] in ("taskgroup", "jiragroup"):
            d.update(tr("[dim]task group — expand and select a task[/dim]"))
            return
        if data["type"] == "jira_stub":
            st = tr("Done") if data.get("done") else tr("not started")
            d.update(tr("[b]%s[/b]  (Jira — %s)\n%s\n\n[dim]n — activate: creates a cc task (worktree + agent), prompt prefilled from Jira[/dim]")
                     % (data["key"], st, data.get("summary", "")))
            return
        if data["type"] == "archive":
            n = sum(1 for e in s["epics"].values() if e["project"] == data["id"] and e.get("archived"))
            d.update(tr("[b]Archive[/b] — %d archived epics in '%s'\n[dim]expand the node; on an epic, x → Unarchive[/dim]") % (n, data["id"]))
            return
        if data["type"] == "ops" and data["id"] in (s.get("ops") or {}):
            o = s["ops"][data["id"]]
            st = _ops_status(o)
            L = [tr("[b]ops: %s[/b]   group %s   %s %s") % (o.get("kind", "?"), o.get("epic", "?"),
                                                          OPS_GLYPH.get(st, "?"), status_label(st))]
            # outcome FROM FACTS (the recorded exit code), not the agent's prose
            if st == "done":
                L.append(tr("[green]✓ success — agent finished with exit 0[/green]"))
            elif st == "failed":
                rc = _ops_exit(o)
                L.append(tr("[bold red]✗ FAILED — exit=%s%s[/bold red]")
                         % (rc, tr(" (agent died without an exit code — crash)") if rc is None else ""))
                L.append(tr("[dim]o — open session to investigate/restart[/dim]"))
            ni = cc.task_needs_input({"log": o.get("log")})
            if ni:
                L += ["", tr("[bold yellow]❓ agent needs an answer:[/bold yellow] %s") % ni,
                      tr("[dim]o — open session and reply (agent will continue)[/dim]")]
            L += ["", tr("[b]log (tail):[/b]")]
            try:
                tail = open(o["log"], encoding="utf-8", errors="replace").read().splitlines()[-15:]
                L += ["  " + ln for ln in tail] if tail else [tr("  [dim](empty)[/dim]")]
            except Exception:
                L += [tr("  [dim](no log yet)[/dim]")]
            L += ["", tr("[dim]o — open/reply to agent[/dim]")]
            d.update("\n".join(L))
            return
        if data["type"] == "task" and data["id"] in s["tasks"]:
            t = s["tasks"][data["id"]]
            L = [tr("[b]%s[/b]   status=%s") % (t["title"], status_label(fast_status(t))),
                 tr("epic: %s    branch: %s") % (t["epic"], t["branch"])]
            if t.get("needs_input"):
                L += ["", tr("[bold yellow]❓ agent needs your answer:[/bold yellow] %s") % t["needs_input"],
                      tr("[dim]o — open chat and reply (agent will continue)[/dim]")]
            L += ["", tr("repos -> target:")]
            for r in t["repos"]:
                L.append("  %s -> %s" % (r, t["base"][r]))
                if t["mrs"].get(r):
                    url = t["mrs"][r]
                    i = len(self._detail_urls)
                    self._detail_urls.append(url)
                    L.append("     [@click=app.open_url_idx(%d)][u]MR: %s[/u][/]" % (i, url))
            skipped = t.get("skipped") or {}
            if skipped:
                L += ["", tr("[yellow]⚠️ repos NOT set up (%d):[/yellow]") % len(skipped)]
                for r, e in skipped.items():
                    L.append("  [yellow]%s — %s[/yellow]" % (r, e))
            if t.get("merged"):
                L += ["", tr("[bold green]✓ all MRs merged — x = clean up worktrees[/bold green]")]
            L += ["", tr("[b]changes:[/b]")]
            cached = self._changes.get(data["id"])
            if cached is None:
                L.append(tr("  [dim]…(r — show local changes)[/dim]"))
            else:
                for r in t["repos"]:
                    st = cached.get(r, "")
                    L.append("  [%s] %s" % (r, st.replace("\n", "; ") if st else tr("(none)")))
            recent = cc.read_audit(task=data["id"], limit=5)   # what cc DID to this task (audit timeline)
            if recent:
                L += ["", tr("[b]timeline:[/b]  [dim](L — full log)[/dim]")]
                for ar in recent:
                    ts = time.strftime("%H:%M %d.%m", time.localtime(ar.get("ts", 0)))
                    L.append("  [dim]%s[/dim] %s" % (ts, _audit_brief(ar)))
            L += ["", tr("[dim]o=chat  c=Cursor  M=create MR  x=remove  G=move to group  L=timeline  P=panel[/dim]")]
            d.update("\n".join(L))
        elif data["type"] == "epic":
            e = s["epics"][data["id"]]
            mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
            mrs = e.get("mrs", {}) or {}
            mst = e.get("mr_state", {}) or {}
            L = [tr("[b]epic %s[/b]  %s   [%s]") % (data["id"], e.get("summary", ""), mode), ""]
            if mode == "epic_branch":
                L += [tr("epic branch: [b]%s[/b]  ->  master/main") % data["id"]]
                rows = [(r, data["id"]) for r in sorted(mrs)]
            else:
                L += [tr("routing (integration branches):")]
                for r, b in e.get("targets", {}).items():
                    L.append("  %s -> %s" % (r, b))
                rows = list((e.get("targets") or {}).items())
            # epic -> master/main MRs (clickable, lazily filled on focus / by g; merged shown too)
            L += ["", tr("[b]MRs to master/main:[/b]")]
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
                    L.append("  [@click=app.open_url_idx(%d)][u]%-20s[/u][/]  [%s]%s[/%s]%s" % (idx, r, c, status_label(st), c, tail))
                    shown += 1
                elif mode == "targets":
                    L.append(tr("  %-20s  [dim]- MR not found (<- %s)[/dim]") % (r, br))
            if not shown:
                hint = tr("[dim]…(g — find MR to master)[/dim]") if data["id"] not in self._epic_synced else tr("[dim]- no MR to master yet[/dim]")
                L += ["  " + hint]
            L += ["", tr("[dim]o=group chat  n=task  P=panel (MR/Test/Stage/Release)  M=group MR→master[/dim]")]
            d.update("\n".join(L))
        elif data["type"] == "project":
            p = s["projects"][data["id"]]
            j = p.get("jira", {})
            jira_line = ("on — %s @ %s" % (j.get("project_key", "?"), j.get("site", "?"))) if j.get("token") else "off"
            L = [tr("[b]project %s[/b]  (%s, %d repos)") % (data["id"], p.get("kind", "—"), len(p.get("repos", {}))),
                 tr("assignee: %s    Jira: %s") % (p.get("default_assignee") or "-", jira_line),
                 "", tr("repos (reviewer):")]
            L += [tr("repos — deploy / reviewer:")]
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
                    env = tr("[dim](D = load deployments)[/dim]")
                L.append("  %-20s %s  ·  rev:%s" % (r, env, ri.get("reviewer") or "—"))
            L += ["", tr("[dim]e=epic  R=reviewers  j=Jira  D=refresh deployments[/dim]")]
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
        self.push_screen(OutputScreen(tr("recover lost tasks"),
                         [sys.executable, "-u", ENGINE, "recover", "--json"], _recovery_lines),
                         lambda _: self.build_tree())

    def action_panel(self):
        # read-only control panel for a group: all its tasks + MR/merge state at a glance + actions
        ekey = self._current_epic()
        if not ekey:
            self.notify(tr("select a group (epic) or its task — P opens the group panel")); return
        epic, rows = _group_panel_rows(self.state(), ekey)
        self.push_screen(GroupPanelScreen(ekey, epic, rows), lambda res: self._panel_action(ekey, res))

    def _panel_action(self, ekey, res):
        # the panel's action buttons. merge_tasks = task MRs -> epic branch (not prod, light confirm);
        # release_mr = epic->master MR via the same MR-confirm gate as `M`.
        if not res:
            return
        act = res.get("action")
        if act == "release_mr":
            argv = [sys.executable, "-u", ENGINE, "epic", "mr", ekey]
            self._mr_with_gate(argv, tr("Epic MR: %s") % ekey, lambda _: self._after_epic_sync(ekey))
        elif act == "merge_tasks":
            def go(ok):
                if not ok:
                    self.notify(tr("cancelled — nothing merged")); return
                self.push_screen(OutputScreen(tr("Merge tasks into %s") % ekey,
                                 [sys.executable, "-u", ENGINE, "epic", "merge", ekey]),
                                 lambda _: self._after_epic_sync(ekey))
            self.push_screen(ConfirmScreen(tr("Merge tasks into epic branch %s") % ekey,
                             tr("Merge OPEN task MRs into the epic branch (this is NOT a production release). Continue?"),
                             ok_label=tr("Merge")), go)
        elif act == "ops":
            # launch a headless ops agent (test/stage) — it studies the repos & acts, asks via ❓
            kind = res.get("kind", "test")
            self.push_screen(OutputScreen(tr("Start ops agent [%s] for %s") % (kind, ekey),
                             [sys.executable, "-u", ENGINE, "epic", "ops", ekey, "--kind", kind]),
                             lambda _: self.build_tree())

    def action_regroup(self):
        # move the focused task to another group on the board (task-based regrouping)
        tid = self._cur_task()
        if not tid:
            self.notify(tr("select a task to move it to another group")); return
        opts, cur = _group_options(self.state(), tid)
        if not opts:
            self.notify(tr("no other groups in this project — create an epic (e)")); return
        def go(val):
            if not val:
                return
            self.push_screen(OutputScreen(tr("regroup %s -> %s") % (tid, val),
                             [sys.executable, "-u", ENGINE, "task", "regroup", tid, val]),
                             lambda _: self.build_tree())
        self.push_screen(MoveToGroupScreen(tid, opts, cur), go)

    def action_timeline(self):
        # the 'what happened' log; context-aware — filtered to the focused task/epic, else everything
        cur = self.current()
        argv = [sys.executable, "-u", ENGINE, "log", "-n", "150", "--json"]
        title = tr("timeline — all")
        if cur and cur.get("type") == "task":
            argv += ["--task", cur["id"]]; title = tr("timeline · task %s") % cur["id"]
        elif cur and cur.get("type") == "epic":
            argv += ["--epic", cur["id"]]; title = tr("timeline · epic %s") % cur["id"]
        self.push_screen(OutputScreen(title, argv, _timeline_lines))

    def action_refresh(self):
        cur = self.current()
        # `r` on an EPIC node = re-sync its children from Jira
        if cur and cur.get("type") == "epic":
            ekey = cur["id"]
            s = self.state()
            proj = s["epics"].get(ekey, {}).get("project")
            if s["projects"].get(proj, {}).get("jira", {}).get("token"):
                self.notify(tr("syncing epic issues from Jira …"))
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
        self.call_from_thread(lambda: self.notify(tr("epic issues updated")))

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
            self.notify(tr("select a project node first"), severity="error"); return
        self.push_screen(ReviewersScreen(proj))

    def action_deploys(self):
        proj = self._current_project()
        if not proj:
            self.notify(tr("select a project"), severity="error"); return
        self.notify(tr("refreshing deployment status (dev/stage/prod) …"))
        self._bg("deploys:%s" % proj, lambda: self._do_deploys(proj))

    def _do_deploys(self, proj):
        subprocess.run([sys.executable, ENGINE, "deploys", proj], capture_output=True)
        self.call_from_thread(self.build_tree)
        self.call_from_thread(lambda: self.notify(tr("deployments updated")))

    def action_project_jira(self):
        proj = self._current_project()
        if not proj:
            self.notify(tr("select a project node first"), severity="error"); return
        self.push_screen(ProjectJiraScreen(proj), lambda _: self.build_tree())

    def _project_added(self, res):
        if not res:
            return
        args = [sys.executable, ENGINE, "project", "add", res["path"]]
        if res["name"]:
            args.append(res["name"])
        self.push_screen(OutputScreen(tr("Add project"), args), lambda _: self.build_tree())

    def action_new_epic(self):
        proj = self._current_project()
        if not proj:
            self.notify(tr("select a project node first"), severity="error")
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
            self.notify(tr("epic %s imported from Jira") % res["key"] if r.returncode == 0
                        else tr("error: %s") % r.stderr.strip()[:100])
        elif res.get("action") == "create":
            r = subprocess.run([sys.executable, ENGINE, "jira", "create-epic", proj, res["summary"]],
                               capture_output=True, text=True)
            m = re.search(r"created Jira epic: (\S+)", r.stdout or "")
            if m:
                subprocess.run([sys.executable, ENGINE, "epic", "add", proj, m.group(1),
                                "--summary", res["summary"]], capture_output=True, text=True)
                self.notify(tr("created Jira epic %s") % m.group(1))
            else:
                self.notify(tr("could not create Jira epic: %s") % ((r.stderr or r.stdout).strip()[:120]), severity="error")
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
            self.notify(tr("epic %s added") % res["key"] if r.returncode == 0
                        else tr("epic failed: %s") % r.stderr.strip()[:120])
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
        # on a PROJECT node -> a task with NO epic (attaches to the project, MR -> master/main).
        # Jira picker (if the project has a token) lets you pull any EXISTING project ticket onto the board.
        if data and data["type"] == "project":
            pname = data["id"]
            jira_on = bool(s["projects"].get(pname, {}).get("jira", {}).get("token"))
            self.push_screen(NewTaskScreen(pname, jira_on=jira_on, project=pname, loose=True), self._task_created)
            return
        epic = self._current_epic()
        if not epic:
            self.notify(tr("select a project, epic, or task first"), severity="error")
            return
        ep = s["epics"].get(epic, {})
        if ep.get("loose"):   # on a loose task -> another loose task in the same project
            pname = ep["project"]
            jira_on = bool(s["projects"].get(pname, {}).get("jira", {}).get("token"))
            self.push_screen(NewTaskScreen(pname, jira_on=jira_on, project=pname, loose=True),
                             self._task_created)
            return
        proj = ep.get("project")
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
        self.push_screen(OutputScreen(tr("task: %s") % res["title"], args),
                         lambda _: self.build_tree())

    # ---- task actions ----
    def _cur_task(self):
        data = self.current()
        if not data or data["type"] != "task":
            self.notify(tr("select a task first"))
            return None
        return data["id"]

    def _pane_alive(self, pane):
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True).stdout
        return pane in out.split()

    def _jira_flags(self, project_name):
        """Launch flags that make a chat use the project's Jira (cc), not the Atlassian MCP — cwd-independent."""
        if not project_name:
            return ""
        proj = self.state()["projects"].get(project_name, {})
        return cc.chat_jira_flags(proj, project_name)

    def _resolve_cwd(self, t):
        """cwd + primary repo for a task. Recovered/older tasks may lack 'primary'/'dir' — fall back to
        the first repo that has a worktree, then to any worktree, so opening a chat never KeyErrors."""
        wts = t.get("worktrees") or {}
        prim = t.get("primary")
        if not prim or prim not in wts:
            prim = next((r for r in (t.get("repos") or []) if r in wts), None) or next(iter(wts), None)
        cwd = t.get("dir")
        if not cwd and prim and wts.get(prim):
            cwd = os.path.dirname(wts[prim])   # task root (parent of the repo worktrees), like a normal task's 'dir'
        return cwd, prim

    def _chat_cmd(self, t):
        cwd, prim = self._resolve_cwd(t)
        sid = ((t.get("claude_session") or {}).get(prim) if prim else None) or (cc.resolve_session(cwd) if cwd else None)
        base = ("claude --resume %s --permission-mode auto" % sid) if sid else "claude --permission-mode auto"
        pname = self.state()["epics"].get(t.get("epic"), {}).get("project")
        return cwd, base + self._jira_flags(pname)

    def _mark_seen(self, tid):
        now = time.time()
        def _set(s):
            if tid in s["tasks"]:
                s["tasks"][tid]["seen_at"] = now
        cc.mutate(_set)

    def _open_chat_tab(self, name, cwd, cmd):
        """Open the chat in a NEW cmux tab (native trackpad scroll + Cmd-W to close)."""
        cm = _cmux_path()
        if not cm:
            open_in_terminal(cwd, cmd); return "terminal"
        try:
            r = subprocess.run([cm, "new-surface", "--type", "terminal", "--focus", "true"],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            open_in_terminal(cwd, cmd); return "terminal"   # cmux hung/failed — never freeze; fall back
        m = re.search(r"surface:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", (r.stdout or "") + (r.stderr or ""))
        ref = m.group(0) if m else None
        if not ref:
            open_in_terminal(cwd, cmd); return "terminal"
        # leading newline first: dismisses any pending interactive shell prompt (e.g. oh-my-zsh
        # "[Y/n] update?") that would otherwise eat the start of the `cd` and land claude in the wrong cwd.
        try:
            subprocess.run([cm, "send", "--surface", ref, "--", "\ncd %s && %s\n" % (shlex.quote(cwd), cmd)],
                           capture_output=True, timeout=10)
            subprocess.run([cm, "rename-tab", "--surface", ref, name], capture_output=True, timeout=10)
        except Exception:
            pass
        return "cmux"

    def _open_ops(self, oid):
        o = (self.state().get("ops") or {}).get(oid)
        if not o:
            self.notify(tr("ops run not found")); return
        cwd = o.get("dir")
        if not cwd or not os.path.isdir(cwd):
            self.notify(tr("ops directory missing"), severity="error"); return
        sid = cc.resolve_session(cwd)
        chat = "claude --resume %s --permission-mode auto" % sid if sid else "claude --permission-mode auto"
        chat += self._jira_flags(self.state()["epics"].get(o.get("epic"), {}).get("project"))
        where = self._open_chat_tab("cc:ops %s" % o.get("kind", ""), cwd, chat)
        self.notify(tr("ops session [%s] opened (%s) — answer ❓ if present") % (o.get("kind", ""), where))

    def action_open(self):
        data = self.current()
        if data and data.get("type") == "ops":
            self._open_ops(data["id"]); return
        if data and data.get("type") == "epic":
            self.action_epic_chat(); return          # `o` on a group opens its chat (folded from `O`)
        if data and data.get("type") == "project":
            self.action_project_setup(); return
        tid = self._cur_task()
        if not tid:
            self.notify(tr("select a task, group, or project — o opens chat"))
            return
        t = self.state()["tasks"][tid]
        cwd, prim = self._resolve_cwd(t)
        if not cwd or not os.path.isdir(cwd):
            self.notify(tr("worktree missing — recreate the task"), severity="error"); return
        self._mark_seen(tid)
        pname = self.state()["epics"].get(t.get("epic"), {}).get("project")
        jflags = self._jira_flags(pname)
        sess = (t.get("claude_session") or {}).get(prim) if prim else None
        self.notify(tr("opening chat %s …") % tid)
        # do the blocking work (task setup + cmux/terminal launch) OFF the main thread — a slow/hung
        # cmux must never freeze the whole TUI (the bug). Daemon thread + call_from_thread for the toast.
        def _open():
            try:
                subprocess.run([sys.executable, ENGINE, "task", "setup", tid], capture_output=True, timeout=60)
            except Exception:
                pass   # stale rules are fine; never block the chat on a slow setup
            sid = sess or cc.resolve_session(cwd)
            chat = ("claude --resume %s --permission-mode auto" % sid) if sid else "claude --permission-mode auto"
            chat += jflags
            where = self._open_chat_tab("cc:%s" % tid, cwd, chat)
            self.call_from_thread(lambda: self.notify(tr("chat %s opened (%s); Cmd-W to close") % (tid, where)))
        self._bg("open:%s" % tid, _open)

    def action_project_setup(self):
        proj = self._current_project()
        if not proj:
            self.notify(tr("select a project"), severity="error"); return
        subprocess.run([sys.executable, ENGINE, "project", "setup", proj], capture_output=True)
        base = self.state()["projects"].get(proj, {}).get("path", "")
        sdir = os.path.join(base, ".cc-setup")
        if not os.path.isdir(sdir):
            self.notify(tr("could not prepare setup chat"), severity="error"); return
        sid = cc.resolve_session(sdir)   # resume the SAME session if one already exists for this chat
        resume = ("--resume %s " % sid) if sid else ""
        cmd = "claude %s--permission-mode auto --add-dir %s%s" % (resume, shlex.quote(base), self._jira_flags(proj))
        where = self._open_chat_tab("cc:setup %s" % proj, sdir, cmd)
        self.notify(tr("project chat '%s' %s (%s)") % (proj, tr("resumed") if sid else tr("opened"), where))

    def action_epic_chat(self):
        ekey = self._current_epic()
        if not ekey:
            self.notify(tr("select an epic (or its task)"), severity="error"); return
        subprocess.run([sys.executable, ENGINE, "epic", "open", ekey], capture_output=True)
        s = self.state()
        e = s["epics"].get(ekey, {})
        proj = s["projects"].get(e.get("project"), {})
        edir = os.path.join(proj.get("path", ""), "cctui", ekey, "_release")
        if not os.path.isdir(edir):
            self.notify(tr("could not prepare epic chat"), severity="error"); return
        repos = e.get("repos") or list(proj.get("repos", {}).keys())
        adds = " ".join("--add-dir %s" % shlex.quote(proj["repos"][r]["path"])
                        for r in repos if proj.get("repos", {}).get(r, {}).get("path"))
        sid = cc.resolve_session(edir)   # resume the SAME epic chat session if it exists
        resume = ("--resume %s " % sid) if sid else ""
        cmd = "claude %s--permission-mode auto %s%s" % (resume, adds, self._jira_flags(e.get("project")))
        where = self._open_chat_tab("cc:epic %s" % ekey, edir, cmd)
        self.notify(tr("epic chat %s %s (%s) — release/coordination") % (ekey, tr("resumed") if sid else tr("opened"), where))

    def _cursor_epic(self, ekey):
        # open the feature COMBINED (ALL its tasks merged, one branch per repo) in Cursor — for
        # debugging the whole feature together / reviewing what the tasks produced.
        if not shutil.which("cursor"):
            self.notify(tr("cursor CLI is not installed (not in PATH)"), severity="error"); return
        st = self.state()
        active = {tid for tid, t in st["tasks"].items() if t.get("epic") == ekey and not t.get("archived")}
        g = st["epics"].get(ekey) or {}
        have = set(g.get("combined") or [])
        if active and not active.issubset(have):   # combined misses tasks → rebuild with the WHOLE feature
            self.notify(tr("combining the whole feature (%d tasks) — Cursor will open automatically…") % len(active))
            def _combine_and_open():
                try:
                    subprocess.run([sys.executable, ENGINE, "group", "combine", ekey, "--all"],
                                   capture_output=True, timeout=600)
                except Exception:
                    pass
                cwts2 = (cc.load_state()["epics"].get(ekey) or {}).get("combined_worktrees") or {}
                if not cwts2:
                    self.call_from_thread(lambda: self.notify(tr("combine failed (conflict/error) — check ⊕"), severity="error")); return
                folder2 = os.path.dirname(next(iter(cwts2.values())))
                subprocess.Popen(["cursor", folder2])
                self.call_from_thread(lambda: self.notify(tr("Cursor: combined feature %s (%d repos)") % (ekey, len(cwts2))))
            self._bg("combine:%s" % ekey, _combine_and_open)
            return
        cwts = g.get("combined_worktrees") or {}
        if not cwts:
            self.notify(tr("no active tasks in this feature — nothing to combine"), severity="error"); return
        folder = os.path.dirname(next(iter(cwts.values())))   # cctui/<key>/__combined__ — все репо merged вместе
        if not os.path.isdir(folder):
            self.notify(tr("combined folder missing — rebuild feature (⊕)"), severity="error"); return
        subprocess.Popen(["cursor", folder])
        self.notify(tr("Cursor: combined feature %s (all tasks together)") % ekey)

    def action_cursor(self):
        data = self.current()
        if data and data.get("type") == "epic":
            self._cursor_epic(data["id"]); return
        tid = self._cur_task()
        if not tid:
            return
        t = self.state()["tasks"].get(tid, {})
        cwd, _ = self._resolve_cwd(t)   # recovered tasks lack 'dir' — derive it from the worktrees
        if not shutil.which("cursor"):
            self.notify(tr("cursor CLI is not installed (not in PATH)"), severity="error"); return
        if not cwd or not os.path.isdir(cwd):
            self.notify(tr("task folder missing — recreate the task"), severity="error"); return
        try:
            subprocess.run([sys.executable, ENGINE, "task", "open", tid], capture_output=True, timeout=30)
        except Exception:
            pass
        subprocess.Popen(["cursor", cwd])
        self.notify(tr("Cursor opened (task folder) for %s") % tid)

    def action_mr_real(self):
        self._mr(False)

    def action_cleanup(self):
        data = self.current()
        if data and data["type"] == "epic":
            ekey = data["id"]
            self.push_screen(EpicManageScreen(ekey), lambda res: self._epic_manage(ekey, res))
            return
        tid = self._cur_task()
        if not tid:
            self.notify(tr("select a task (Done/Abort) or epic (Done and remove from board)"))
            return
        self.push_screen(TaskManageScreen(tid), lambda res: self._task_manage(tid, res))

    def _task_manage(self, tid, res):
        if not res:
            return
        if res == "done":
            self.push_screen(OutputScreen(tr("Done and remove: %s") % tid,
                             [sys.executable, "-u", ENGINE, "task", "done", tid]),
                             lambda _: self.build_tree())
        elif res == "abort":
            self.push_screen(OutputScreen(tr("abort: %s") % tid,
                             [sys.executable, "-u", ENGINE, "task", "abort", tid]),
                             lambda _: self.build_tree())

    def _epic_manage(self, ekey, res):
        if res != "done":
            return
        # Jira(эпик+задачи)->Done + локальная уборка эпика и задач из cc — стримим вывод
        self.push_screen(OutputScreen(tr("Done and remove: %s (+ Jira Done)") % ekey,
                         [sys.executable, "-u", ENGINE, "epic", "done", ekey]),
                         lambda _: self.build_tree())

    def _mr(self, dry):
        data = self.current()
        if not data:
            self.notify(tr("select a task or epic"))
            return
        if data["type"] == "task":
            argv = [sys.executable, "-u", ENGINE, "task", "mr", data["id"]]; title = "MR: %s" % data["id"]
        elif data["type"] == "epic":
            argv = [sys.executable, "-u", ENGINE, "epic", "mr", data["id"]]; title = tr("Epic MR: %s") % data["id"]
        else:
            self.notify(tr("select a task (MR) or epic (epic->master MR)"))
            return
        ekey = data["id"] if data["type"] == "epic" else None
        after = (lambda _: (self._after_epic_sync(ekey) if ekey else self.build_tree()))
        if dry:
            self.push_screen(OutputScreen(title + " (dry)", argv + ["--dry-run"]), after)
            return
        self._mr_with_gate(argv, title, after)

    def _mr_with_gate(self, argv, title, after):
        # REAL MR creation -> confirm WHERE it goes first (esp. prod-bound master/main), then create.
        # Shared by the `M` key and the panel's Release button.
        def go(ok):
            if not ok:
                self.notify(tr("MR cancelled — nothing created"))
                return
            self.push_screen(OutputScreen(title, argv), after)
        self.push_screen(MRConfirmScreen(tr("Create MR — %s") % title, argv + ["--dry-run"]), go)


if __name__ == "__main__":
    CCApp().run()
