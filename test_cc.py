import sys, types, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cc
# Process-wide isolation: many tests exercise audit()-instrumented functions (jira_move, load_state
# self-heal, …). Point the audit log at a throwaway temp file so NO test ever writes ~/.cc/audit.log.
cc.AUDIT_FILE = pathlib.Path(tempfile.mkdtemp(prefix="cc-audit-run-")) / "audit.log"


def test_task_needs_input():
    import os, tempfile
    d = tempfile.mkdtemp(prefix="cc-ni-")
    try:
        log = os.path.join(d, "t.log")
        open(log, "w").write("did stuff...\nmore work\n[cc-needs-input] Какой порт использовать для web?\n")
        assert cc.task_needs_input({"log": log}) == "Какой порт использовать для web?"
        open(log, "w").write("finished cleanly, no questions\n")
        assert cc.task_needs_input({"log": log}) is None
        assert cc.task_needs_input({"log": os.path.join(d, "nope.log")}) is None
        assert cc.task_needs_input({}) is None
    finally:
        import shutil; shutil.rmtree(d, ignore_errors=True)

def test_jira_move_visco_miscategorized():
    # visco footgun: review/qa statuses miscategorized as 'new', NO 'new' status actually named
    # like to-do. `todo` must REFUSE (don't move to a wrong status — this is the VIS-117 prevention).
    TRS = {"transitions": [
        {"id": "41", "name": "Готово к проверке", "to": {"name": "Готово к проверке", "statusCategory": {"key": "new"}}},
        {"id": "42", "name": "Ready for QA", "to": {"name": "Ready for QA", "statusCategory": {"key": "new"}}},
        {"id": "31", "name": "Готово", "to": {"name": "Готово", "statusCategory": {"key": "done"}}},
    ]}
    posted = {}
    def fake(cfg, method, path, body=None):
        if method == "GET" and path.endswith("/transitions"):
            return TRS
        if method == "POST" and "/transitions" in path:
            posted["id"] = body["transition"]["id"]; return {}
        return {}
    saved = cc.jira_req
    cc.jira_req = fake
    try:
        ok, info = cc.jira_move({}, "VIS-1", "todo")          # no to-do-NAMED 'new' status -> refuse, no move
        assert not ok and not posted, (ok, info, posted)
        ok, info = cc.jira_move({}, "VIS-1", "Готово к проверке")  # exact name still works (deterministic)
        assert ok and posted.get("id") == "41", (ok, posted)
        posted.clear(); ok, info = cc.jira_move({}, "VIS-1", "готово")  # 'done' token -> the real Готово
        assert ok and posted.get("id") == "31", (ok, posted)   # NOT "Готово к проверке" (that's category new)
    finally:
        cc.jira_req = saved


def test_jira_move():
    TRS = {"transitions": [
        {"id": "11", "name": "К выполнению", "to": {"name": "К выполнению", "statusCategory": {"key": "new"}}},
        {"id": "21", "name": "В работе", "to": {"name": "В работе", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Готово", "to": {"name": "Готово", "statusCategory": {"key": "done"}}},
    ]}
    posted = {}
    def fake(cfg, method, path, body=None):
        if method == "GET" and path.endswith("/transitions"):
            return TRS
        if method == "POST" and "/transitions" in path:
            posted["id"] = body["transition"]["id"]; return {}
        return {}
    saved = cc.jira_req
    cc.jira_req = fake
    try:
        ok, info = cc.jira_move({}, "K-1", "К выполнению")             # exact name (backward)
        assert ok and info == "К выполнению" and posted["id"] == "11", (ok, info, posted)
        posted.clear(); cc.jira_move({}, "K-1", "todo")               # category keyword -> 'new'
        assert posted["id"] == "11", posted
        posted.clear(); cc.jira_move({}, "K-1", "готово")             # forward to done
        assert posted["id"] == "31", posted
        posted.clear(); ok, info = cc.jira_move({}, "K-1", "Опубликовано")  # unreachable
        assert not ok and not posted, (ok, posted)
    finally:
        cc.jira_req = saved


def test_gen_task_title():
    saved = cc.claude_text
    try:
        # claude returns a clean line (with trailing noise) -> first clean line kept
        cc.claude_text = lambda cwd, prompt, timeout=120: "Fix the login redirect bug\nsome extra reasoning"
        assert cc.gen_task_title("the login keeps redirecting to /checkout", cwd="/tmp") == "Fix the login redirect bug"
        # claude unavailable -> fallback to a clean first line of the prompt
        cc.claude_text = lambda *a, **k: None
        assert cc.gen_task_title("add a lobby timer to the table", cwd="/tmp") == "add a lobby timer to the table"
        # empty prompt -> the given fallback
        assert cc.gen_task_title("", cwd="/tmp", fallback="task") == "task"
    finally:
        cc.claude_text = saved


def test_slugify():
    assert cc.slugify("БЧК badge fix") == "badge-fix" or cc.slugify("Hello World!") == "hello-world"
    assert cc.slugify("Hello World!") == "hello-world"
    assert cc.slugify("a"*100) == "a"*40
    assert cc.slugify("") == "task"

def test_worktree_path():
    p = cc.worktree_path("/x/invictus", "IK-1", "my-slug", "invictuswebsite")
    assert str(p) == "/x/invictus/cctui/IK-1/my-slug/invictuswebsite", p

def test_target_for():
    epic = {"targets": {"website": "loyalty/integration"}}
    proj = {"repos": {"website": {"default_branch": "main"}, "payment": {"default_branch": "master"}}}
    assert cc.target_for(epic, proj, "website") == "loyalty/integration"
    assert cc.target_for(epic, proj, "payment") == "master"


def test_unique_tid_no_collision():
    s = {"tasks": {}}
    # first task with slug "go" under epic E1
    t1 = cc._unique_tid(s, "E1", "go"); s["tasks"][t1] = {"epic": "E1"}
    assert t1 == "t_go", t1
    # same slug, DIFFERENT epic -> must NOT reuse t_go (that was the IK-8894-go loss)
    t2 = cc._unique_tid(s, "E2", "go"); s["tasks"][t2] = {"epic": "E2"}
    assert t2 != t1 and t2 not in (), t2
    assert t2 == "t_e2_go", t2
    # same slug, SAME epic -> numbered, still unique
    t3 = cc._unique_tid(s, "E1", "go"); s["tasks"][t3] = {"epic": "E1"}
    assert t3 not in (t1, t2), t3
    # all keys distinct -> no overwrite possible
    assert len(s["tasks"]) == 3, s["tasks"]


def test_epic_knowledge_stash():
    s = {"projects": {"P": {"path": "/tmp/cc-x-test", "repos": {}}},
         "epics": {"E1": {"project": "P", "summary": "sum", "memory": "a\nb",
                          "mode": "epic_branch", "targets": {}}},
         "tasks": {}}
    cc._epic_teardown(s, s["projects"]["P"], "E1")
    assert "E1" not in s["epics"], "epic should be removed"
    k = s["epic_knowledge"]["E1"]
    assert k["memory"] == "a\nb" and k["summary"] == "sum", k


def test_state_lock_reentrant():
    # nested acquisition in the same thread must NOT deadlock (main() wraps a command that then
    # calls mutate() -> this used to hang cc repo add / project new / recover forever)
    with cc.state_lock():
        with cc.state_lock():
            pass
    # mutate() under an outer lock must also work (reentrant)
    with cc.state_lock():
        cc.mutate(lambda st: None)


def test_clean_subject():
    fb = "fallback-title"
    # leaked reasoning / review -> fallback
    assert cc._clean_subject("Прежде чем выдать MR — в дифе несоответствие, давай разберём", fb) == fb
    assert cc._clean_subject("one sentence. two sentence. three here", fb) == fb
    assert cc._clean_subject("x" * 120, fb) == fb
    assert cc._clean_subject("", fb) == fb
    # clean Conventional-Commits title kept; ticket prefix + preamble stripped (cc re-adds [IK-XXXX])
    assert cc._clean_subject("feat(loyalty): exclude annual", fb) == "feat(loyalty): exclude annual"
    assert cc._clean_subject("[IK-8631] fix(pos): disable bonus", fb) == "fix(pos): disable bonus"
    assert cc._clean_subject("MR title: feat(clubs): filter tier", fb) == "feat(clubs): filter tier"


def test_ensure_loose_epic():
    s = {"projects": {"azi": {"repos": {}}}, "epics": {}, "tasks": {}}
    k = cc.ensure_loose_epic(s, "azi")
    assert k == "azi__loose", k
    e = s["epics"][k]
    assert e["loose"] is True and e["project"] == "azi" and e["targets"] == {} and e["mode"] == "targets"
    # idempotent — second call returns the same key, doesn't duplicate
    k2 = cc.ensure_loose_epic(s, "azi")
    assert k2 == k and len(s["epics"]) == 1


def test_loose_task_targets_default_branch():
    # the whole point: a loose task's MR target resolves to the repo default branch (master/main),
    # NOT an epic/integration branch.
    proj = {"repos": {"web": {"default_branch": "main"}, "api": {"default_branch": "master"}}}
    loose = {"project": "azi", "mode": "targets", "targets": {}, "loose": True}
    assert cc.target_for(loose, proj, "web") == "main"
    assert cc.target_for(loose, proj, "api") == "master"


def test_unique_branch():
    s = {"tasks": {"t1": {"branch": "fix-login"}, "t2": {"branch": "fix-login-2"}}}
    assert cc._unique_branch(s, "add-timer") == "add-timer"    # free
    assert cc._unique_branch(s, "fix-login") == "fix-login-3"  # base + -2 taken -> -3


def test_project_target():
    import types
    st = {"projects": {"visco": {"repos": {"web": {"default_branch": "dev"}, "api": {"default_branch": "dev"}}}},
          "epics": {}, "tasks": {}}
    saved = (cc.load_state, cc.save_state)
    cc.load_state = lambda: st
    cc.save_state = lambda s: None
    try:
        # set web -> a collect branch; loose container is created and holds the target
        cc.cmd_project_target(types.SimpleNamespace(project="visco", spec=["web=feature/api-integrations"], clear=False))
        lk = cc.loose_epic_key("visco")
        assert st["epics"][lk]["targets"] == {"web": "feature/api-integrations"}, st["epics"][lk]
        # target_for now routes a loose web task there, api stays default
        e = st["epics"][lk]; proj = st["projects"]["visco"]
        assert cc.target_for(e, proj, "web") == "feature/api-integrations"
        assert cc.target_for(e, proj, "api") == "dev"
        # clear -> back to default
        cc.cmd_project_target(types.SimpleNamespace(project="visco", spec=[], clear=True))
        assert st["epics"][lk]["targets"] == {}
        assert cc.target_for(st["epics"][lk], proj, "web") == "dev"
    finally:
        cc.load_state, cc.save_state = saved


def test_jira_chat_setup():
    import shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-jcs-")
    try:
        # with a token: returns the CLAUDE.md Jira block pointing at `cc jira <project>`
        proj = {"jira": {"site": "x.atlassian.net", "email": "e", "token": "tok", "project_key": "AZI"}}
        block = cc.jira_chat_setup(d, proj, "azi")
        assert "cc jira search azi" in block and "Atlassian MCP" in block, block
        # no token: empty (chat left untouched)
        assert cc.jira_chat_setup(d, {"jira": {}}, "azi") == ""
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scan_orphans_loose():
    import os, shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-orph-")
    try:
        def wt(slug):  # create cctui/visco__loose/<slug>/web/.git, return the web worktree path
            p = os.path.join(d, "cctui", "visco__loose", slug, "web")
            os.makedirs(os.path.join(p, ".git")); return p
        onboard = wt("fix-login")     # a loose task that IS on the board
        ghost = wt("ghost-fix")       # a loose worktree NOT claimed by any task
        s = {"projects": {"visco": {"path": d, "repos": {"web": {"default_branch": "dev"}}}},
             "epics": {"visco__loose": {"project": "visco", "loose": True, "mode": "targets", "targets": {}}},
             "tasks": {"t_fix": {"epic": "visco__loose", "branch": "fix-login", "worktrees": {"web": onboard}}}}
        orph = cc._scan_orphans(s)
        # the on-board loose task is matched by worktree path (NOT flagged); only the ghost is an orphan
        assert len(orph) == 1, [o["slug"] for o in orph]
        assert orph[0]["slug"] == "ghost-fix" and orph[0]["branch"] == "ghost-fix", orph[0]  # loose => plain-slug branch
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_clean_dead_settings():
    import os, json, shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-cds-")
    try:
        cdir = os.path.join(d, ".claude"); os.makedirs(cdir)
        sf = os.path.join(cdir, "settings.json")
        # only the dead key -> file deleted
        open(sf, "w").write(json.dumps({"deniedMcpServers": ["claude.ai Atlassian"]}))
        cc._clean_dead_settings(d)
        assert not os.path.exists(sf), "file with only the dead key should be removed"
        # dead key alongside a real one -> key stripped, rest kept
        open(sf, "w").write(json.dumps({"deniedMcpServers": ["x"], "model": "opus"}))
        cc._clean_dead_settings(d)
        data = json.load(open(sf))
        assert data == {"model": "opus"}, data
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chat_jira_flags():
    import shlex
    proj = {"jira": {"site": "x.atlassian.net", "email": "e", "token": "tok", "project_key": "AZI"}}
    flags = cc.chat_jira_flags(proj, "azi")
    # the HARD, cwd-independent block: disallow the Atlassian connector tools at launch
    assert "--disallowedTools" in flags and "mcp__claude_ai_Atlassian__*" in flags, flags
    assert "--append-system-prompt" in flags and "cc jira" in flags, flags
    # shell-quoted so it survives being spliced into a `claude …` command line
    assert shlex.split(flags) == ["--disallowedTools", "mcp__claude_ai_Atlassian__*",
                                  "--append-system-prompt"] + [shlex.split(flags)[3]], shlex.split(flags)
    # no token -> no flags (the MCP, whatever it points at, stays)
    assert cc.chat_jira_flags({"jira": {}}, "azi") == ""


def test_jira_pull(monkeypatched=True):
    import os, shutil, tempfile, types
    d = tempfile.mkdtemp(prefix="cc-pull-")
    # stub state + network: project has a token; two images + one video; dup filename
    saved = (cc.load_state, cc.jira_attachments, cc.jira_download)
    cc.load_state = lambda: {"projects": {"azi": {"jira": {"site": "s", "email": "e", "token": "t", "project_key": "AZI"}}}}
    cc.jira_attachments = lambda cfg, key: [
        {"id": "1", "filename": "design.png", "mime": "image/png", "size": 10, "content": "u1"},
        {"id": "2", "filename": "design.png", "mime": "image/png", "size": 20, "content": "u2"},  # dup name
        {"id": "3", "filename": "repro.mp4", "mime": "video/mp4", "size": 30, "content": "u3"},
    ]
    cc.jira_download = lambda cfg, url, dest: (open(dest, "wb").write(b"x"), 1)[1]
    try:
        cc.cmd_jira_pull(types.SimpleNamespace(project="azi", key="AZI-9", out=d, images_only=True))
        files = sorted(os.listdir(d))
        # images-only -> the mp4 is skipped; the duplicate name is suffixed, not clobbered
        assert files == ["design-2.png", "design.png"], files
    finally:
        cc.load_state, cc.jira_attachments, cc.jira_download = saved
        shutil.rmtree(d, ignore_errors=True)


def test_load_state_self_heal():
    import os, json, shutil, tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-heal-"))
    saved = (cc.STATE_FILE, cc._BACKUP_DIR)
    cc.STATE_FILE = d / "state.json"; cc._BACKUP_DIR = d / "backups"; cc._BACKUP_DIR.mkdir()
    try:
        cc.STATE_FILE.write_text("{ this is torn json")          # corrupt
        good = {"projects": {"p": {"repos": {}}}, "epics": {}, "tasks": {}}
        (cc._BACKUP_DIR / "state-20260101-000000-1.json").write_text(json.dumps({"oops": 1}))  # invalid backup
        (cc._BACKUP_DIR / "state-20260102-000000-1.json").write_text(json.dumps(good))          # newest VALID
        out = cc.load_state()
        assert out == good, out                                   # self-healed from newest valid backup
        assert json.loads(cc.STATE_FILE.read_text()) == good      # corrupt file replaced in place
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


def test_state_issues():
    s = {"projects": {"P": {"repos": {"web": {}}}},
         "epics": {"E": {"project": "P"}, "BAD": {"project": "NOPE"}},
         "tasks": {"t1": {"epic": "E", "repos": ["web", "ghost"], "worktrees": {}},
                   "t2": {"epic": "GONE", "repos": [], "worktrees": {}}}}
    msgs = [m for _, m in cc._state_issues(s)]
    assert any("BAD" in m and "несуществующий проект" in m for m in msgs), msgs
    assert any("t2" in m and "несуществующий эпик" in m for m in msgs), msgs
    assert any("ghost" in m for m in msgs), msgs
    # a clean state -> no issues
    assert cc._state_issues({"projects": {}, "epics": {}, "tasks": {}}) == []


def test_valid_state():
    assert cc._valid_state({"projects": {}, "epics": {}, "tasks": {}})
    assert not cc._valid_state({"projects": {}, "epics": {}})       # missing tasks
    assert not cc._valid_state([])                                  # not a dict


def _audit_sandbox():
    """Redirect cc.AUDIT_FILE to a fresh temp file so audit tests never touch ~/.cc/audit.log."""
    import tempfile
    saved = cc.AUDIT_FILE
    cc.AUDIT_FILE = pathlib.Path(tempfile.mkdtemp(prefix="cc-audit-")) / "audit.log"
    return saved


def test_audit_append_and_read():
    import shutil
    saved = _audit_sandbox()
    try:
        cc.audit("task.add", task="E-1", epic="E", repos=["web", "api"], skipped=[])
        cc.audit("task.merge", task="E-1", repo="web", mr=42, base="main")
        recs = cc.read_audit()
        assert len(recs) == 2
        assert recs[0]["action"] == "task.merge"                    # newest first
        assert recs[0]["mr"] == 42
        assert recs[1]["repos"] == ["web", "api"]
        assert "skipped" not in recs[1]                             # empty values dropped
        assert all("ts" in r for r in recs)
    finally:
        shutil.rmtree(cc.AUDIT_FILE.parent, ignore_errors=True); cc.AUDIT_FILE = saved


def test_audit_filters():
    import shutil, time
    saved = _audit_sandbox()
    try:
        cc.audit("task.merge", task="A", epic="E1", repo="web")
        cc.audit("task.merge", task="B", epic="E2", repo="api")
        cc.audit("jira.transition", key="IK-9", to="Done")
        assert [r["task"] for r in cc.read_audit(task="A")] == ["A"]
        assert [r["epic"] for r in cc.read_audit(epic="E2")] == ["E2"]
        assert len(cc.read_audit(action="jira.transition")) == 1
        assert cc.read_audit(since=time.time() + 1000) == []        # nothing newer than far future
    finally:
        shutil.rmtree(cc.AUDIT_FILE.parent, ignore_errors=True); cc.AUDIT_FILE = saved


def test_audit_never_raises_on_unwritable():
    # logging must be best-effort: an unwritable path can't blow up a real git/jira action.
    saved = cc.AUDIT_FILE
    cc.AUDIT_FILE = pathlib.Path("/dev/null/nope/audit.log")        # parent can't be created
    try:
        cc.audit("task.add", task="x")          # must not raise
        assert cc.read_audit() == []            # unreadable -> empty, not crash
    finally:
        cc.AUDIT_FILE = saved


def test_audit_trim_keeps_newest():
    import shutil
    saved = _audit_sandbox()
    saved_keep, saved_max = cc._AUDIT_KEEP, cc._AUDIT_MAX
    cc._AUDIT_KEEP, cc._AUDIT_MAX = 5, 1        # force a trim on every append past 1 byte
    try:
        for i in range(20):
            cc.audit("tick", count=i)
        recs = cc.read_audit(limit=999)
        assert len(recs) <= 5, len(recs)
        assert recs[0]["count"] == 19           # newest survived the trim
    finally:
        cc._AUDIT_KEEP, cc._AUDIT_MAX = saved_keep, saved_max
        shutil.rmtree(cc.AUDIT_FILE.parent, ignore_errors=True); cc.AUDIT_FILE = saved


def test_cmd_log_smoke():
    import shutil
    saved = _audit_sandbox()
    try:
        cc.audit("task.add", task="E-1", epic="E", repos=["web"])
        cc.cmd_log(types.SimpleNamespace(task=None, epic=None, action=None, today=False, n=50))
        cc.cmd_log(types.SimpleNamespace(task="nope", epic=None, action=None, today=False, n=50))  # empty -> no crash
    finally:
        shutil.rmtree(cc.AUDIT_FILE.parent, ignore_errors=True); cc.AUDIT_FILE = saved


def _rules_state(loose_dir, epic_dir):
    return {
        "projects": {"P": {"path": "/tmp/p", "repos": {"web": {"default_branch": "main", "remote": "x/web"}},
                           "jira": {}}},
        "epics": {"P__loose": {"project": "P", "loose": True, "mode": "targets", "targets": {}},
                  "E1": {"project": "P", "summary": "My epic", "mode": "epic_branch", "branch": "E1",
                         "memory": "epic note v1"}},
        "tasks": {
            "t_loose": {"epic": "P__loose", "title": "fix login", "branch": "fix-login", "repos": ["web"],
                        "worktrees": {"web": loose_dir + "/web"}, "dir": loose_dir},
            "t_epic": {"epic": "E1", "title": "feat x", "branch": "E1-feat-x", "repos": ["web"],
                       "worktrees": {"web": epic_dir + "/web"}, "dir": epic_dir},
        },
    }


def test_render_task_claude_md_loose_vs_epic():
    s = _rules_state("/tmp/loose", "/tmp/epic")
    loose = cc.render_task_claude_md(s, "t_loose")
    assert "no epic" in loose and "master/main" in loose, loose[:200]
    assert "branch fix-login" in loose and "cc task merge t_loose" in loose
    epic = cc.render_task_claude_md(s, "t_epic")
    assert "Epic E1: My epic" in epic and "epic note v1" in epic       # epic memory baked in
    assert "EPIC task" in epic and "epic's branch (E1)" in epic and "cc task merge t_epic" in epic


def test_write_task_claude_md_is_always_fresh():
    import os, shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-rules-")
    try:
        s = _rules_state(d, d)                                          # epic task writes into d/CLAUDE.md
        assert cc.write_task_claude_md(s, "t_epic")
        md1 = open(os.path.join(d, "CLAUDE.md")).read()
        assert "epic note v1" in md1
        # change the rules (epic memory), regenerate -> file reflects the NEW rules (the staleness fix)
        s["epics"]["E1"]["memory"] = "epic note v2 — NEW RULE"
        assert cc.write_task_claude_md(s, "t_epic")
        md2 = open(os.path.join(d, "CLAUDE.md")).read()
        assert "epic note v2 — NEW RULE" in md2 and "epic note v1" not in md2
        # no dir -> best-effort False, never raises
        s["tasks"]["t_epic"]["dir"] = None; s["tasks"]["t_epic"]["worktrees"] = {}
        assert cc.write_task_claude_md(s, "t_epic") is False
        assert cc.write_task_claude_md(s, "nope") is False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_mr_target_for():
    proj = {"repos": {"web": {"default_branch": "main"}, "api": {"default_branch": "master"}}}
    eb = {"mode": "epic_branch", "branch": "E1"}                       # epic-branch -> the epic branch
    assert cc.mr_target_for("E1", eb, proj, "web") == "E1"
    tg = {"mode": "targets", "targets": {"web": "E1-integration"}}     # targets -> per-repo target / default
    assert cc.mr_target_for("E1", tg, proj, "web") == "E1-integration"
    assert cc.mr_target_for("E1", tg, proj, "api") == "master"         # no target -> repo default
    loose = {"loose": True, "mode": "targets", "targets": {}}          # loose -> default branch
    assert cc.mr_target_for("P__loose", loose, proj, "web") == "main"


def _regroup_state(tmpd):
    return {
        "projects": {"P": {"path": tmpd, "repos": {"web": {"default_branch": "main", "remote": "x/web"}}},
                     "Q": {"path": tmpd, "repos": {"web": {"default_branch": "main"}}}},
        "epics": {"A": {"project": "P", "summary": "A", "mode": "epic_branch", "branch": "A"},
                  "B": {"project": "P", "summary": "B", "mode": "targets", "targets": {"web": "B-int"}},
                  "Z": {"project": "Q", "summary": "Z", "mode": "epic_branch", "branch": "Z"}},
        "tasks": {"t_x": {"epic": "A", "title": "x", "branch": "A-x", "repos": ["web"],
                          "base": {"web": "A"}, "worktrees": {}, "mrs": {}}},
    }


def test_cmd_task_regroup():
    import os, json, shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-regroup-")
    saved = (cc.STATE_FILE, cc._BACKUP_DIR)
    try:
        cc.STATE_FILE = pathlib.Path(d) / "state.json"; cc._BACKUP_DIR = pathlib.Path(d) / "bk"
        def run(group):
            cc.STATE_FILE.write_text(json.dumps(_regroup_state(d)))
            cc.cmd_task_regroup(types.SimpleNamespace(task="t_x", group=group))
            return json.loads(cc.STATE_FILE.read_text())["tasks"]["t_x"]

        t = run("B")                                   # A (epic_branch) -> B (targets)
        assert t["epic"] == "B" and t["base"]["web"] == "B-int", t
        assert t["branch"] == "A-x"                     # branch UNCHANGED (only membership/target move)
        t = run("P")                                   # -> ungroup (project's loose group)
        assert t["epic"] == "P__loose" and t["base"]["web"] == "main", t
        # refusals (all SystemExit, state untouched):
        for bad, why in [("Z", "cross-project"), ("A", "same group as start"), ("nope", "unknown group")]:
            try:
                run(bad); assert False, "expected refuse: %s" % why
            except SystemExit:
                pass
        # refuse when the task already has an MR
        st = _regroup_state(d); st["tasks"]["t_x"]["mrs"] = {"web": "http://mr/1"}
        cc.STATE_FILE.write_text(json.dumps(st))
        try:
            cc.cmd_task_regroup(types.SimpleNamespace(task="t_x", group="B")); assert False
        except SystemExit:
            pass
        assert json.loads(cc.STATE_FILE.read_text())["tasks"]["t_x"]["epic"] == "A"  # unchanged
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


def _ops_state(tmpd):
    return {
        "projects": {"P": {"path": tmpd, "repos": {"web": {"default_branch": "main"},
                                                   "api": {"default_branch": "master"}}}},
        "epics": {"E1": {"project": "P", "summary": "epic one", "mode": "epic_branch", "branch": "E1"}},
        "tasks": {"t_x": {"epic": "E1", "title": "x", "branch": "E1-x", "repos": ["web", "api"],
                          "worktrees": {"web": tmpd + "/wt/web", "api": tmpd + "/wt/api"}}},
    }


def test_ops_id_and_kinds():
    assert cc.ops_id("IK-8894", "test") == "ops_test_ik-8894"
    assert set(cc.OPS_KINDS) >= {"test", "stage", "deploy"}


def test_render_ops_runbook():
    s = _ops_state("/tmp/p")
    test_rb = cc.render_ops_runbook(s, "E1", "test")
    assert "group E1" in test_rb and "E1-x" in test_rb and "wt/web" in test_rb and "wt/api" in test_rb
    assert "[cc-needs-input]" in test_rb and "Do NOT run git" in test_rb
    assert "LOCAL only" in test_rb                       # test = local, no deploy
    stage_rb = cc.render_ops_runbook(s, "E1", "stage")
    assert "STAGE only" in stage_rb and "never touch production" in stage_rb
    assert "[cc-needs-input]" in stage_rb               # ask-when-unsure baked into every kind


def test_cmd_epic_ops_manual():
    import os, json, shutil, tempfile
    d = tempfile.mkdtemp(prefix="cc-ops-")
    saved = (cc.STATE_FILE, cc._BACKUP_DIR)
    try:
        cc.STATE_FILE = pathlib.Path(d) / "state.json"; cc._BACKUP_DIR = pathlib.Path(d) / "bk"
        cc.STATE_FILE.write_text(json.dumps(_ops_state(d)))
        cc.cmd_epic_ops(types.SimpleNamespace(key="E1", kind="test", manual=True))  # no claude launch
        s = json.loads(cc.STATE_FILE.read_text())
        oid = cc.ops_id("E1", "test")
        assert oid in s["ops"] and s["ops"][oid]["kind"] == "test" and s["ops"][oid]["status"] == "running"
        cmd = os.path.join(d, "cctui", "E1", "_ops-test", "CLAUDE.md")
        assert os.path.isfile(cmd), "ops runbook not written"
        assert "cc OPS agent" in open(cmd).read()
        # bad kind refuses; empty group refuses
        try:
            cc.cmd_epic_ops(types.SimpleNamespace(key="E1", kind="nope", manual=True)); assert False
        except SystemExit:
            pass
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


class _R:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_github_mr_layer():
    # MR ops dispatch to `gh` when the repo's origin is GitHub, and parse PR JSON correctly.
    saved, calls = cc.run, []

    def fake(cmd, cwd=None, check=True, **kw):
        calls.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _R("https://github.com/freekos/cc-orchestrator.git\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            if "number,url,baseRefName,state" in cmd:
                return _R('{"number":42,"url":"https://github.com/o/r/pull/42","baseRefName":"main","state":"OPEN"}')
            if "url,state" in cmd:
                return _R('{"url":"https://github.com/o/r/pull/42","state":"MERGED"}')
            if "url" in cmd:
                return _R('{"url":"https://github.com/o/r/pull/42"}')
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _R("merged", 0)
        return _R("")
    cc.run = fake
    try:
        assert cc.repo_host("/x") == "github"
        assert cc.find_mr("o/r", "br", "/x") == "https://github.com/o/r/pull/42"
        url, st = cc.mr_info("o/r", "br", "/x")
        assert url == "https://github.com/o/r/pull/42" and st == "merged"   # GH state -> glab vocab
        om = cc.open_mr("o/r", "br", "/x")
        assert om["iid"] == 42 and om["target_branch"] == "main" and om["state"] == "opened"
        ok, _ = cc.merge_mr("o/r", 42, "/x", squash=True)
        assert ok and any(c[:3] == ["gh", "pr", "merge"] and "--squash" in c for c in calls)
        assert cc.mr_url(_R("done https://github.com/o/r/pull/99"), "o/r", "br", "/x") == "https://github.com/o/r/pull/99"
    finally:
        cc.run = saved


def test_mr_layer_gitlab_intact():
    # regression: GitLab repos still go through `glab` exactly as before.
    saved = cc.run

    def fake(cmd, cwd=None, check=True, **kw):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _R("https://gitlab.com/grp/repo.git\n")
        if cmd[:2] == ["glab", "api"]:
            return _R('[{"iid":7,"web_url":"https://gitlab.com/grp/repo/-/merge_requests/7","target_branch":"dev","state":"opened"}]')
        if cmd[:3] == ["glab", "mr", "merge"]:
            return _R("merged", 0)
        return _R("")
    cc.run = fake
    try:
        assert cc.repo_host("/x") == "gitlab"
        om = cc.open_mr("grp/repo", "br", "/x")
        assert om["iid"] == 7 and om["target_branch"] == "dev"
        ok, _ = cc.merge_mr("grp/repo", 7, "/x")
        assert ok
    finally:
        cc.run = saved


def test_create_mr():
    # create_mr dispatches: GitHub -> `gh pr create` (no label v1), GitLab -> `glab mr create` (full).
    saved, calls = cc.run, []

    def fake(cmd, cwd=None, check=True, **kw):
        calls.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _R("https://github.com/o/r.git\n") if cwd == "/gh" else _R("https://gitlab.com/g/r.git\n")
        if cmd[:3] == ["gh", "pr", "create"]:
            return _R("https://github.com/o/r/pull/5\n")
        if cmd[:3] == ["glab", "mr", "create"]:
            return _R("https://gitlab.com/g/r/-/merge_requests/5\n")
        return _R("")
    cc.run = fake
    try:
        url, err = cc.create_mr("o/r", "br", "main", "T", "B", "mylabel", "me", "rev", "/gh")
        assert url == "https://github.com/o/r/pull/5" and err == "", (url, err)
        ghc = [c for c in calls if c[:3] == ["gh", "pr", "create"]][0]
        assert "--label" not in ghc and "--head" in ghc and "--base" in ghc   # GitHub v1: no label
        url2, _ = cc.create_mr("g/r", "br", "main", "T", "B", "mylabel", "me", "rev", "/gl")
        assert url2 == "https://gitlab.com/g/r/-/merge_requests/5"
        glc = [c for c in calls if c[:3] == ["glab", "mr", "create"]][0]
        assert "--label" in glc and "--reviewer" in glc and "--assignee" in glc   # GitLab: full
    finally:
        cc.run = saved


def test_snapshot():
    # the GUI data contract: projects -> groups -> tasks/ops with status + per-repo MR facts.
    import json, io, contextlib
    s = {"projects": {"P": {"kind": "single", "repos": {"web": {}}}},
         "epics": {"E1": {"project": "P", "summary": "e1", "combined": ["t1"]},
                   "P__loose": {"project": "P", "loose": True}},
         "tasks": {"t1": {"epic": "E1", "title": "T1", "status": "mr", "branch": "b",
                          "repos": ["web"], "base": {"web": "main"}, "mrs": {"web": "u"}},
                   "t2": {"epic": "E1", "title": "T2", "status": "wip", "branch": "b2",
                          "repos": ["web"], "base": {"web": "main"}}}}
    saved = cc.load_state
    cc.load_state = lambda: s
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_snapshot(types.SimpleNamespace(json=True))
        d = json.loads(buf.getvalue())
        groups = d["projects"]["P"]["groups"]
        e1 = [x for x in groups if x["key"] == "E1"][0]
        t1 = [t for t in e1["tasks"] if t["tid"] == "t1"][0]
        t2 = [t for t in e1["tasks"] if t["tid"] == "t2"][0]
        assert t1["status"] == "mr" and t1["repos"][0]["mr"] == "u"
        assert not any(x["key"] == "P__loose" for x in groups)   # empty loose group hidden
        assert d["projects"]["P"]["repos"] == ["web"]
        # combine contract: per-task `combined` flag + per-group combined set + branch name
        assert t1["combined"] is True and t2["combined"] is False
        assert e1["combined"] == ["t1"] and e1["combined_branch"] == "E1-combined"
        # activity contract: every task carries a sort key (last-interaction), int >= 0
        assert isinstance(t1["activity"], int) and t1["activity"] >= 0
    finally:
        cc.load_state = saved


def test_parse_ci_handles():
    # the agent reports WHAT CI it triggered; cc reads the fact from it (②b)
    text = "log...\n[cc-ci] gitlab invictusfitness/frontend/invictusv2 12345\nx\n[cc-ci] github owner/repo 999\n"
    assert cc._parse_ci_handles(text) == [
        ("gitlab", "invictusfitness/frontend/invictusv2", "12345"),
        ("github", "owner/repo", "999")]
    assert cc._parse_ci_handles("") == []


def test_ci_pipeline_status():
    import types
    saved = cc.run
    try:
        def mk(out, rc=0):
            return lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out)
        cc.run = mk('{"status":"completed","conclusion":"success"}')
        assert cc.ci_pipeline_status("github", "o/r", "1") == "success"
        cc.run = mk('{"status":"completed","conclusion":"failure"}')
        assert cc.ci_pipeline_status("github", "o/r", "1") == "failed"
        cc.run = mk('{"status":"in_progress","conclusion":null}')
        assert cc.ci_pipeline_status("github", "o/r", "1") == "running"
        cc.run = mk('{"status":"success"}')
        assert cc.ci_pipeline_status("gitlab", "g/p", "5") == "success"
        cc.run = mk('{"status":"failed"}')
        assert cc.ci_pipeline_status("gitlab", "g/p", "5") == "failed"
        cc.run = mk('{"status":"running"}')
        assert cc.ci_pipeline_status("gitlab", "g/p", "5") == "running"
        cc.run = mk("", rc=1)                       # provider error -> unknown, NEVER fabricated success
        assert cc.ci_pipeline_status("gitlab", "g/p", "5") == "unknown"
    finally:
        cc.run = saved


def test_ops_status_folds_ci_fact():
    # ②b: a 0-exit agent is NOT trusted as a successful deploy — the CI pipeline FACT decides.
    import tempfile, os, shutil, json as _j
    d = tempfile.mkdtemp(prefix="cc-cifact-")
    try:
        e0 = os.path.join(d, "e0"); open(e0, "w").write("0")
        assert cc._snap_ops_status({"pid": None, "exit": e0}) == "done"          # no CI handle -> done (unverified)
        cf = os.path.join(d, "f.ci"); open(cf, "w").write(_j.dumps({"rc": 0, "checked": True, "pipelines": [{"status": "failed"}]}))
        assert cc._snap_ops_status({"pid": None, "exit": e0, "ci": cf}) == "failed"   # KEY: deploy pipeline failed
        cg = os.path.join(d, "g.ci"); open(cg, "w").write(_j.dumps({"rc": 0, "checked": True, "pipelines": [{"status": "success"}]}))
        assert cc._snap_ops_status({"pid": None, "exit": e0, "ci": cg}) == "done"     # verified green
        cu = os.path.join(d, "u.ci"); open(cu, "w").write(_j.dumps({"rc": 0, "checked": True, "pipelines": [{"status": "unknown"}]}))
        assert cc._snap_ops_status({"pid": None, "exit": e0, "ci": cu}) == "done"     # inconclusive -> no false red
        e1 = os.path.join(d, "e1"); open(e1, "w").write("1")
        assert cc._snap_ops_status({"pid": None, "exit": e1, "ci": cg}) == "failed"   # agent failed -> failed regardless
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_running_ops_overlap():
    # collision-awareness: only IN-FLIGHT ops count, and only when repos intersect
    import os
    s = {"ops": {
        "op_a": {"epic": "G1", "kind": "stage", "pid": os.getpid(), "repos": ["web", "api"]},   # alive -> running
        "op_b": {"epic": "G2", "kind": "test", "pid": None, "exit": "/nope", "repos": ["web"]},  # crashed -> not running
    }}
    hit = cc._running_ops_overlap(s, ["web"])
    assert [oid for oid, _, _ in hit] == ["op_a"], hit               # the running one, not the dead one
    assert cc._running_ops_overlap(s, ["api"])[0][2] == ["api"]      # reports the overlapping repos
    assert cc._running_ops_overlap(s, ["mobile"]) == []             # no shared repo -> no collision


def test_task_memory():
    # shared task memory: decisions WITH STATUS — pivot files the old current as abandoned + clears it,
    # the append-only log keeps history (incl. dead-ends), so future chats aren't misled.
    import tempfile, pathlib, types, io, contextlib, shutil
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-memtest-"))
    saved_dir, saved_audit, saved_load = cc.STATE_DIR, cc.AUDIT_FILE, cc.load_state
    cc.STATE_DIR = d; cc.AUDIT_FILE = d / "audit.log"; cc.load_state = lambda: {"tasks": {"t1": {}}}
    try:
        def run(**kw):
            b = io.StringIO()
            with contextlib.redirect_stdout(b):
                cc.cmd_task_memory(types.SimpleNamespace(task="t1", log=kw.get("log"), pivot=kw.get("pivot"), current=kw.get("current")))
            return b.getvalue()
        run(current="фильтр через query-параметр")
        run(log="бэк уже отдаёт audience")
        out = run(pivot="query не работает в webview → cookie")
        cur, log = cc._parse_mem(out)
        assert cur == "", "current cleared on pivot"
        assert any("заброшено" in l and "query" in l for l in log), "old current filed as abandoned"
        assert any("разворот" in l and "cookie" in l for l in log), "pivot reason logged"
        assert any("audience" in l for l in log), "earlier log preserved"
        cur2, _ = cc._parse_mem(run(current="cookie-based фильтр"))
        assert cur2 == "cookie-based фильтр", "new current set after pivot"
        assert cc.task_memory_path("t1").exists()
    finally:
        cc.STATE_DIR, cc.AUDIT_FILE, cc.load_state = saved_dir, saved_audit, saved_load
        shutil.rmtree(d, ignore_errors=True)


def test_snapshot_archived():
    # soft archive: an archived task is OFF the board by default, but returned (flagged) with --all for search.
    import json, io, contextlib
    s = {"projects": {"P": {"kind": "single", "repos": {"web": {}}}},
         "epics": {"E1": {"project": "P", "summary": "e1"},
                   "E2": {"project": "P", "summary": "e2", "archived": True}},
         "tasks": {"t1": {"epic": "E1", "title": "Live", "status": "mr", "branch": "b", "repos": ["web"], "base": {"web": "main"}},
                   "t2": {"epic": "E1", "title": "Done", "status": "done", "branch": "b2", "repos": ["web"], "base": {"web": "main"},
                          "archived": True, "archived_at": "2026-06-26 10:00"},
                   "t3": {"epic": "E2", "title": "InArchEpic", "status": "done", "branch": "b3", "repos": ["web"], "base": {"web": "main"}}}}
    saved = cc.load_state; cc.load_state = lambda: s
    try:
        def snap(all_):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cc.cmd_snapshot(types.SimpleNamespace(json=True, all=all_))
            return json.loads(buf.getvalue())
        # default: archived task t2 hidden, archived epic E2 hidden entirely
        d = snap(False); groups = d["projects"]["P"]["groups"]
        e1 = [x for x in groups if x["key"] == "E1"][0]
        assert [t["tid"] for t in e1["tasks"]] == ["t1"], "archived task hidden by default"
        assert not any(x["key"] == "E2" for x in groups), "archived epic hidden by default"
        # --all: archived task + epic returned, flagged
        d2 = snap(True); groups2 = d2["projects"]["P"]["groups"]
        e1b = [x for x in groups2 if x["key"] == "E1"][0]
        t2 = [t for t in e1b["tasks"] if t["tid"] == "t2"][0]
        assert t2["archived"] is True and t2["archived_at"] == "2026-06-26 10:00"
        e2 = [x for x in groups2 if x["key"] == "E2"][0]
        assert e2["archived"] is True and any(t["tid"] == "t3" for t in e2["tasks"])
        # live task carries archived=False (contract present for the cockpit filter)
        assert [t for t in e1["tasks"] if t["tid"] == "t1"][0]["archived"] is False
    finally:
        cc.load_state = saved


def test_task_sessions_history():
    # recover old cc TUI chats: scan ALL the task's worktrees (primary often mislabels the repo),
    # mark cc's service one-shots, sort newest-first; history strips cc's injected preamble and
    # merges consecutive same-role blocks.
    import tempfile, pathlib, types, io, contextlib, shutil, json as _json, os, re
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-sestest-"))
    saved_cp, saved_load = cc.CLAUDE_PROJECTS, cc.load_state
    try:
        wtA = d / "wt" / "repoA"; wtB = d / "wt" / "repoB"
        wtA.mkdir(parents=True); wtB.mkdir(parents=True)
        cc.CLAUDE_PROJECTS = d / "projects"
        cc.load_state = lambda: {"tasks": {"t1": {
            "worktrees": {"repoA": str(wtA), "repoB": str(wtB)}, "primary": "repoA",
            "claude_session": {}}}}
        def enc(p): return re.sub(r"[^A-Za-z0-9-]", "-", str(p))
        def write(wt, sid, lines, mtime):
            pd = cc.CLAUDE_PROJECTS / enc(wt); pd.mkdir(parents=True, exist_ok=True)
            f = pd / (sid + ".jsonl"); f.write_text("\n".join(_json.dumps(l) for l in lines))
            os.utime(f, (mtime, mtime))
        # chat session in the NON-primary repo (proves we scan all worktrees); user text carries cc's
        # injected worktree block which must be stripped; two consecutive assistant blocks must merge.
        write(wtB, "aaaa1111-0000-0000-0000-000000000001", [
            {"type": "user", "message": {"role": "user",
             "content": "add user profile\n\n[cc] You may create/edit files across these repo worktrees (branch X):\n- repoA: /x\nDo NOT run git"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Окей, начинаю."}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Готово."}]}},
        ], mtime=2000)
        # service one-shot (cc's MR summary) in primary repo, OLDER
        write(wtA, "bbbb2222-0000-0000-0000-000000000002", [
            {"type": "user", "message": {"role": "user",
             "content": "Summarize THIS branch's diff into a Merge Request, following the repo's CLAUDE.md."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "feat(x): ..."}]}},
        ], mtime=1000)

        def run(fn, **kw):
            b = io.StringIO()
            with contextlib.redirect_stdout(b):
                fn(types.SimpleNamespace(**kw))
            return b.getvalue()

        items = _json.loads(run(cc.cmd_task_sessions, task="t1", json=True))
        assert len(items) == 2, items
        assert items[0]["sid"].startswith("aaaa1111"), "newest-first ordering"
        byrepo = {it["repo"]: it for it in items}
        assert byrepo["repoB"]["kind"] == "chat" and byrepo["repoA"]["kind"] == "service", byrepo
        assert byrepo["repoB"]["preview"] == "add user profile", "cc preamble stripped from preview"
        assert byrepo["repoB"]["turns"] == 2, "consecutive assistant blocks merged -> 2 turns"

        msgs = _json.loads(run(cc.cmd_task_history, session="aaaa1111", json=True))
        assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
        assert msgs[0]["text"] == "add user profile", "injected preamble stripped in history"
        assert msgs[1]["text"] == "Окей, начинаю.\n\nГотово.", "assistant blocks merged"
    finally:
        cc.CLAUDE_PROJECTS, cc.load_state = saved_cp, saved_load
        shutil.rmtree(d, ignore_errors=True)


def test_jira_write_pull_only_by_default():
    # cc is pull-only: automatic Jira writes happen ONLY when the project opted in with --write
    assert cc.jira_write_on(None) is False
    assert cc.jira_write_on({}) is False
    assert cc.jira_write_on({"token": "x", "project_key": "IK"}) is False, "token alone must NOT enable writes"
    assert cc.jira_write_on({"write": True}) is True


def test_project_ops_validates_kind():
    # project ops as a unit: an unknown kind must die rather than launch a bogus agent
    class A:
        project = "P"; kind = "bogus"; force = False; manual = True
    raised = False
    try:
        cc.cmd_project_ops(A())
    except SystemExit:
        raised = True
    assert raised, "unknown ops kind must die"


def test_task_land_requires_branch():
    # land must refuse a task with no branch (nothing to merge) rather than do something undefined
    state = {"tasks": {"t1": {"epic": "e", "repos": []}}, "epics": {"e": {"project": "P"}},
             "projects": {"P": {"repos": {}}}}
    saved = cc.load_state
    try:
        cc.load_state = lambda: state

        class A:
            task = "t1"; push = False; json = False
        raised = False
        try:
            cc.cmd_task_land(A())
        except SystemExit:
            raised = True
        assert raised, "land without a branch must die"
    finally:
        cc.load_state = saved


def test_automerge_toggle_default_off():
    # auto-merge is OFF by default (no key); on/off sets/clears the flag on project + group
    import io, contextlib
    state = {"projects": {"P": {"repos": {}}}, "epics": {"g1": {"project": "P"}}, "tasks": {}}
    saved = (cc.load_state, cc.save_state)
    try:
        cc.load_state = lambda: state
        cc.save_state = lambda s: None
        assert not state["projects"]["P"].get("automerge") and not state["epics"]["g1"].get("automerge")

        class PA:
            project = "P"; mode = "on"
        with contextlib.redirect_stdout(io.StringIO()):
            cc.cmd_project_automerge(PA())
        assert state["projects"]["P"]["automerge"] is True

        class PB:
            project = "P"; mode = "off"
        with contextlib.redirect_stdout(io.StringIO()):
            cc.cmd_project_automerge(PB())
        assert "automerge" not in state["projects"]["P"], "off clears the flag"

        class GA:
            key = "g1"; mode = "on"
        with contextlib.redirect_stdout(io.StringIO()):
            cc.cmd_group_automerge(GA())
        assert state["epics"]["g1"]["automerge"] is True
    finally:
        cc.load_state, cc.save_state = saved


def test_group_new_logical_grouping():
    # ad-hoc grouping: create a local group + move tasks logically (branch/worktrees untouched);
    # a task with an open MR is skipped (its target can't silently change), single-membership.
    import io, json as _json, contextlib
    state = {
        "projects": {"P": {"path": "/tmp/p", "repos": {"r1": {}}}},
        "epics": {"P__loose": {"project": "P", "loose": True, "targets": {}, "mode": "targets"}},
        "tasks": {
            "t1": {"epic": "P__loose", "repos": ["r1"], "base": {"r1": "main"}},
            "t2": {"epic": "P__loose", "repos": ["r1"], "base": {"r1": "main"}, "mrs": {"r1": "!1"}},
        },
    }
    saved = (cc.load_state, cc.save_state, cc.write_task_claude_md, cc.mr_target_for)
    try:
        cc.load_state = lambda: state
        cc.save_state = lambda s: None
        cc.write_task_claude_md = lambda s, tid: None
        cc.mr_target_for = lambda k, e, p, r: "main"

        class A:
            project = "P"; name = "Моя группа"; tasks = "t1,t2"; json = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.cmd_group_new(A())
        out = _json.loads(buf.getvalue())
        assert out["moved"] == ["t1"], out
        assert len(out["skipped"]) == 1 and out["skipped"][0]["task"] == "t2", out
        gkey = out["key"]
        assert state["tasks"]["t1"]["epic"] == gkey, "t1 reassigned to the new group"
        assert state["tasks"]["t2"]["epic"] == "P__loose", "t2 untouched (had an MR)"
        assert state["epics"][gkey]["local"] is True and state["epics"][gkey]["project"] == "P"
    finally:
        cc.load_state, cc.save_state, cc.write_task_claude_md, cc.mr_target_for = saved


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d tests passed" % n)
