"""Engine integration (layer 2): run cc's real git logic against THROWAWAY git repos (no network, no
agents). Covers the class of bugs that hit us — default_branch read from the wrong place (-> 11531-commit
garbage MRs), worktree provisioning off the wrong base, orphan detection. Pure stdlib + git; no textual.
"""
import os, shutil, subprocess, tempfile, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cc


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


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d engine-integration tests passed" % n)
