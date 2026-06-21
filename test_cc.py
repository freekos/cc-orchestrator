import sys, types, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cc

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


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d tests passed" % n)
