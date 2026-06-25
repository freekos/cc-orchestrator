"""Engine integration (layer 2): run cc's real git logic against THROWAWAY git repos (no network, no
agents). Covers the class of bugs that hit us — default_branch read from the wrong place (-> 11531-commit
garbage MRs), worktree provisioning off the wrong base, orphan detection. Pure stdlib + git; no textual.
"""
import os, shutil, subprocess, tempfile, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cc
cc.AUDIT_FILE = pathlib.Path(tempfile.mkdtemp(prefix="cc-audit-eng-")) / "audit.log"  # isolate the audit log


def _g(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_repo(base, name, default="main"):
    """A working clone whose origin/HEAD points at `default` (bare origin + clone + initial commit)."""
    bare, work = base / (name + ".git"), base / name
    subprocess.run(["git", "init", "--bare", "-b", default, str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _g(["config", "user.email", "t@t"], work); _g(["config", "user.name", "t"], work)
    (work / "README.md").write_text("init\n")
    _g(["add", "-A"], work); _g(["commit", "-m", "init"], work)
    _g(["push", "origin", default], work)
    _g(["remote", "set-head", "origin", default], work)   # set refs/remotes/origin/HEAD
    return work


def test_default_branch_reads_origin_head_not_current():
    # regression: cc must take the repo's TRUE default (origin/HEAD), NOT the checked-out branch
    # (the bug that stored feature branches as default and produced whole-history MRs).
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-eng-"))
    try:
        work = _make_repo(d, "repo", "main")
        _g(["checkout", "-b", "feature/x"], work)            # current branch != default
        assert cc.default_branch(str(work)) == "main", cc.default_branch(str(work))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_provision_worktree_off_default_branch():
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-prov-"))
    try:
        work = _make_repo(d, "web", "main")
        proj = {"path": str(d), "repos": {"web": {"path": str(work), "default_branch": "main", "remote": ""}}}
        r, wt, tgt, err = cc._provision("G1", {"targets": {}}, proj, "web", "fix-thing", "fix-thing",
                                        "targets", True)   # no_setup=True
        assert err is None, err
        assert tgt == "main", tgt                            # MR target = default branch
        assert wt and os.path.isdir(wt), wt                  # worktree created on disk
        cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt,
                             capture_output=True, text=True).stdout.strip()
        assert cur == "fix-thing", cur                       # on the task branch
        # ...and that branch was cut from the default's commit
        base_sha = subprocess.run(["git", "rev-parse", "origin/main"], cwd=wt,
                                  capture_output=True, text=True).stdout.strip()
        head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt,
                                  capture_output=True, text=True).stdout.strip()
        assert base_sha == head_sha, "task branch not cut from origin/main"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scan_orphans_finds_unclaimed_worktree():
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-orph-"))
    try:
        work = _make_repo(d, "web", "main")
        proj = {"path": str(d), "repos": {"web": {"path": str(work), "default_branch": "main", "remote": ""}}}
        cc._provision("G1", {"targets": {}}, proj, "web", "ghost-task", "ghost-task", "targets", True)
        # state knows the project + repo but NOT the task -> the on-disk worktree is an orphan
        s = {"projects": {"P": proj}, "epics": {}, "tasks": {}}
        orph = cc._scan_orphans(s)
        assert any(o["slug"] == "ghost-task" for o in orph), [o["slug"] for o in orph]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cmd_task_add_end_to_end_manual():
    # the WHOLE cmd_task_add path on real repos (manual mode = no agent launch). Catches regressions
    # like the full_prompt building a now-out-of-scope repo_map (NameError on real task creation).
    import json, types
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-taskadd-"))
    saved = (cc.STATE_FILE, cc._BACKUP_DIR)
    try:
        web = _make_repo(d, "web", "main")
        state = {"projects": {"P": {"path": str(d), "kind": "multi",
                                    "repos": {"web": {"path": str(web), "default_branch": "main", "remote": "x/web"}}}},
                 "epics": {}, "tasks": {}}
        cc.STATE_FILE = d / "state.json"
        cc._BACKUP_DIR = d / "backups"
        cc.STATE_FILE.write_text(json.dumps(state))
        ns = types.SimpleNamespace(epic="P", title="fix thing", prompt="do the fix",
                                   repos=None, no_setup=True, manual=True, sync=False, jira=None, no_jira=True)
        cc.cmd_task_add(ns)                                      # must NOT raise
        s = json.loads(cc.STATE_FILE.read_text())
        assert len(s["tasks"]) == 1
        tid, t = next(iter(s["tasks"].items()))
        assert t["status"] == "review" and t.get("manual"), t
        assert t["base"]["web"] == "main", t["base"]            # loose task -> default branch
        assert os.path.isdir(t["worktrees"]["web"]), t["worktrees"]
        cmd = os.path.join(t["dir"], "CLAUDE.md")
        assert os.path.isfile(cmd), "CLAUDE.md not written"
        md = open(cmd).read()
        assert "no epic" in md and ("branch %s" % t["branch"]) in md   # fresh rules rendered
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


def test_group_combine_rebuild_from_base():
    # the #1 feature: combine tasks INTO a group's branch, and REMOVE one cleanly (rebuild-from-base,
    # not revert) — removed task's commits vanish, others stay. Real git.
    import json, types
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-combine-"))
    saved = (cc.STATE_FILE, cc._BACKUP_DIR)
    try:
        web = _make_repo(d, "web", "main")
        # two task branches off main, each adds a distinct file (no conflict)
        _g(["checkout", "-b", "t1"], web); (web / "a.txt").write_text("A\n"); _g(["add", "-A"], web); _g(["commit", "-m", "a"], web)
        _g(["checkout", "main"], web)
        _g(["checkout", "-b", "t2"], web); (web / "b.txt").write_text("B\n"); _g(["add", "-A"], web); _g(["commit", "-m", "b"], web)
        _g(["checkout", "main"], web)
        state = {"projects": {"P": {"path": str(d), "repos": {"web": {"path": str(web), "default_branch": "main", "remote": ""}}}},
                 "epics": {"G": {"project": "P", "combined": []}},
                 "tasks": {"t1": {"epic": "G", "branch": "t1", "repos": ["web"]},
                           "t2": {"epic": "G", "branch": "t2", "repos": ["web"]}}}
        cc.STATE_FILE = d / "state.json"; cc._BACKUP_DIR = d / "bk"
        cc.STATE_FILE.write_text(json.dumps(state))

        cc.cmd_group_combine(types.SimpleNamespace(group="G", add="t1", remove=None))
        cc.cmd_group_combine(types.SimpleNamespace(group="G", add="t2", remove=None))
        assert (web / "a.txt").exists() and (web / "b.txt").exists(), "both tasks combined"
        assert "G-combined" in subprocess.run(["git", "branch", "--show-current"], cwd=web, capture_output=True, text=True).stdout

        # remove t1 -> rebuild -> a.txt gone, b.txt stays (clean removal, the proof)
        cc.cmd_group_combine(types.SimpleNamespace(group="G", add=None, remove="t1"))
        assert not (web / "a.txt").exists(), "removed task's changes must vanish (rebuild-from-base)"
        assert (web / "b.txt").exists(), "other task's changes must remain"
        assert json.loads(cc.STATE_FILE.read_text())["epics"]["G"]["combined"] == ["t2"]
    finally:
        cc.STATE_FILE, cc._BACKUP_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d engine-integration tests passed" % n)
