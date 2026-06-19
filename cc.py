#!/usr/bin/env python3
"""cc — multi-repo, epic-routed AI-agent orchestrator (Phase 1 engine).

Model:  Project > Repos   and   Project > Epic > Task > RepoWork
Rule:   the agent only EDITS files; cc owns ALL git (branch/commit/push/MR).
State:  ~/.cc/state.json holds intent + pointers; git/jsonl hold the truth.
"""
import argparse, base64, fcntl, json, os, re, shlex, shutil, subprocess, sys, time, urllib.request
from contextlib import contextmanager
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".cc"
STATE_FILE = STATE_DIR / "state.json"
WS_DIR = STATE_DIR / "workspaces"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

def glab_user():
    """Current GitLab username (for default MR assignee)."""
    r = run(["glab", "api", "user"], check=False)
    try:
        return json.loads(r.stdout).get("username", "")
    except Exception:
        return ""

def glab_members(remote):
    """Project members [{username, name, access}] sorted by access desc — for picking a reviewer."""
    enc = (remote or "").replace("/", "%2F")
    r = run(["glab", "api", "projects/%s/members/all?per_page=100" % enc], check=False)
    try:
        ms = json.loads(r.stdout)
        ms.sort(key=lambda m: -m.get("access_level", 0))
        return [{"username": m.get("username", ""), "name": m.get("name", ""),
                 "access": m.get("access_level", 0)} for m in ms if m.get("username")]
    except Exception:
        return []

# ----------------------------- state -----------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"projects": {}, "epics": {}, "tasks": {}}

def save_state(s):
    """Atomic write (tmp + os.replace) so concurrent readers never see a torn file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    try:
        os.chmod(tmp, 0o600)          # may hold a Jira API token
    except OSError:
        pass
    os.replace(tmp, STATE_FILE)       # atomic on POSIX

_LOCK_PATH = STATE_DIR / ".state.lock"

@contextmanager
def state_lock():
    """Exclusive cross-process lock so load->modify->save can't lose updates.
    Held only around mutations; reads stay lock-free (writes are atomic)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()

def mutate(fn):
    """Run fn(state) under the lock against a FRESH read, then save. Returns fn's result."""
    with state_lock():
        s = load_state()
        r = fn(s)
        save_state(s)
    return r

# ----------------------------- helpers -----------------------------

class _FakeProc:
    returncode = 1
    stdout = ""
    def __init__(self, msg=""):
        self.stderr = msg

def run(cmd, cwd=None, check=True, timeout=120):
    if cwd is not None and not os.path.isdir(str(cwd)):
        if check:
            raise RuntimeError("cwd missing: %s" % cwd)
        return _FakeProc("cwd missing: %s" % cwd)
    try:
        r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # never block forever: a hung git/glab must surface as a normal failure, else a
        # TUI status worker stuck here keeps the app from exiting (asyncio joins it for 300s)
        if check:
            raise RuntimeError("cmd timed out after %ss: %s" % (timeout, " ".join(cmd)))
        return _FakeProc("timed out after %ss: %s" % (timeout, " ".join(cmd)))
    if check and r.returncode != 0:
        raise RuntimeError("cmd failed: %s\n%s" % (" ".join(cmd), r.stderr.strip()))
    return r

def git(args, cwd, check=True, timeout=120):
    return run(["git"] + args, cwd=cwd, check=check, timeout=timeout)

def tmux_name(tid):
    return "cc_" + tid

def tmux_alive(name):
    return bool(shutil.which("tmux")) and subprocess.run(
        ["tmux", "has-session", "-t", name], capture_output=True).returncode == 0

def default_branch(repo_path):
    r = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    for b in ("main", "master"):
        if run(["git", "rev-parse", "--verify", "origin/" + b], cwd=repo_path, check=False).returncode == 0:
            return b
    cur = run(["git", "branch", "--show-current"], cwd=repo_path, check=False).stdout.strip()
    return cur or "HEAD"

def have_ref(repo_path, ref):
    return run(["git", "rev-parse", "--verify", ref], cwd=repo_path, check=False).returncode == 0

def ensure_epic_branch(repo_path, key, base):
    if have_ref(repo_path, key):
        return                                  # already local -> no network
    git(["fetch", "origin", base], cwd=repo_path, check=False)
    for cand in ("origin/" + key, "origin/" + base, base, "HEAD"):
        if have_ref(repo_path, cand):
            git(["branch", key, cand], cwd=repo_path, check=False)
            return
    git(["branch", key], cwd=repo_path, check=False)

def push_epic_branch(repo_path, key):
    run(["git", "push", "--no-verify", "-u", "origin", key], cwd=repo_path, check=False)

def slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s[:40] or "task"

def die(msg):
    print("error: " + msg, file=sys.stderr)
    sys.exit(1)

# ----------------------------- detection -----------------------------

def repo_info(p):
    p = Path(p)
    url = git(["remote", "get-url", "origin"], cwd=p, check=False).stdout.strip()
    clean = url[:-4] if url.endswith(".git") else url
    m = re.search(r"[:/]([^/]+(?:/[^/]+)+)$", clean)
    remote = m.group(1) if m else clean
    provider = "gitlab" if "gitlab" in url else ("github" if "github" in url else "unknown")
    db = git(["symbolic-ref", "--short", "HEAD"], cwd=p, check=False).stdout.strip() or "main"
    setup, run_cmd = detect_setup_run(p)
    return {"path": str(p), "provider": provider, "remote": remote, "default_branch": db,
            "setup": setup, "run": run_cmd, "reviewer": ""}

def detect_setup_run(repo_path):
    """Best-effort defaults so a fresh worktree is runnable.
    node repos: symlink node_modules from the main checkout (instant, shares deps) + a run cmd."""
    pj = Path(repo_path) / "package.json"
    if pj.exists():
        run_cmd = ""
        try:
            scripts = json.loads(pj.read_text()).get("scripts", {})
            run_cmd = "npm run dev" if "dev" in scripts else ("npm start" if "start" in scripts else "")
        except Exception:
            pass
        return 'ln -sfn "$CC_MAIN_REPO/node_modules" node_modules', run_cmd
    if (Path(repo_path) / "go.mod").exists():
        return "", "go run ."
    if (Path(repo_path) / "requirements.txt").exists() or (Path(repo_path) / "pyproject.toml").exists():
        return "", ""
    return "", ""

def detect_repos(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        die("not a directory: %s" % path)
    if (path / ".git").exists():
        return "single", {path.name: repo_info(path)}, path
    repos = {}
    for child in sorted(path.iterdir()):
        if child.name in ("cctui", "cc-orchestrator"):
            continue
        if child.is_dir() and (child / ".git").is_dir():
            repos[child.name] = repo_info(child)
    if not repos:
        die("no git repos at %s (neither a repo nor a folder of repos)" % path)
    return "multi", repos, path

# ----------------------------- session capture -----------------------------

def newest_session_after(ts):
    newest, best = None, ts
    if not CLAUDE_PROJECTS.exists():
        return None
    for f in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt >= best:
            best, newest = mt, f
    return newest.stem if newest else None

def resolve_session(primary_wt):
    """Find the newest claude session transcript for a worktree (lazy, on open)."""
    enc = str(primary_wt).replace("/", "-")
    d = CLAUDE_PROJECTS / enc
    if not d.exists():
        return None
    js = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return js[0].stem if js else None

# ----------------------------- project cmds -----------------------------

def cmd_project_add(args):
    s = load_state()
    kind, repos, path = detect_repos(args.path)
    name = args.name or path.name
    s["projects"][name] = {"path": str(path), "kind": kind, "repos": repos,
                           "default_assignee": glab_user()}
    save_state(s)
    print("project '%s' added - %s, %d repo(s)  (assignee=%s):" % (
        name, kind, len(repos), s["projects"][name]["default_assignee"] or "-"))
    for rn, ri in repos.items():
        print("  - %-22s %-8s %s" % (rn, ri["provider"], ri["remote"]))

def cmd_project_ls(args):
    s = load_state()
    if not s["projects"]:
        print("(no projects - `cc project add <path>`)"); return
    for name, p in s["projects"].items():
        print("%s  [%s, %d repo(s)]  %s" % (name, p["kind"], len(p["repos"]), p["path"]))
        for rn in p["repos"]:
            print("    - " + rn)

# ----------------------------- epic cmds -----------------------------

def cmd_epic_add(args):
    s = load_state()
    if args.project not in s["projects"]:
        die("unknown project '%s'" % args.project)
    proj = s["projects"][args.project]
    targets = {}
    for t in (args.target or []):
        if "=" not in t:
            die("--target must be repo=branch, got '%s'" % t)
        repo, br = t.split("=", 1)
        if repo not in proj["repos"]:
            die("repo '%s' not in project '%s'" % (repo, args.project))
        targets[repo] = br
    erepos = None
    if args.repos:
        erepos = args.repos.split(",")
        for r in erepos:
            if r not in proj["repos"]:
                die("repo '%s' not in project '%s'" % (r, args.project))
    mode = "targets" if targets else "epic_branch"
    s["epics"][args.key] = {"project": args.project, "summary": args.summary or "",
                            "targets": targets, "mode": mode, "branch": args.key, "repos": erepos,
                            "memory": (args.summary or "").strip()}
    n = sync_epic_children(s, args.key)
    save_state(s)
    print("epic '%s' added under '%s'  [mode=%s]" % (args.key, args.project, mode))
    if n > 0:
        print("  pulled %d Jira child issue(s) — visible as stubs under the epic (activate with n)" % n)
    if targets:
        print("  routing (repo -> target branch):")
        for r, b in targets.items():
            print("    %-22s -> %s" % (r, b))
    else:
        print("  epic-branch mode: tasks merge into branch '%s'; that branch -> MR to master/main" % args.key)

def cmd_epic_ls(args):
    s = load_state()
    for key, e in s["epics"].items():
        if args.project and e["project"] != args.project:
            continue
        mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
        print("%s  [%s, %s]  %s" % (key, e["project"], mode, e.get("summary", "")))
        if mode == "epic_branch":
            print("    tasks -> branch '%s' -> MR to master/main" % key)
        for r, b in e.get("targets", {}).items():
            print("    %-22s -> %s" % (r, b))

# ----------------------------- task cmds -----------------------------

def target_for(epic, proj, repo):
    return epic.get("targets", {}).get(repo) or proj["repos"][repo]["default_branch"]

def worktree_path(project_path, epic, slug, repo_name):
    return Path(project_path) / "cctui" / epic / slug / repo_name

def _provision(epic_key, epic, proj, r, branch, slug, epic_mode, no_setup):
    ri = proj["repos"][r]; rp = ri["path"]
    if ri.get("provider") in (None, "unknown") or not ri.get("remote"):
        return (r, None, None, "no git remote")
    wt = worktree_path(proj["path"], epic_key, slug, r)
    # self-heal leftovers from a clobbered/aborted add: stale worktree registrations
    # (dir gone) and orphaned worktree dirs would otherwise skip the repo ("worktree exists")
    git(["worktree", "prune"], cwd=rp, check=False)
    if wt.exists():
        git(["worktree", "remove", "--force", str(wt)], cwd=rp, check=False)
        git(["worktree", "prune"], cwd=rp, check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        if epic_mode == "epic_branch":
            ensure_epic_branch(rp, epic_key, default_branch(rp))
            mr_target, start = epic_key, epic_key
        else:
            mr_target = epic.get("targets", {}).get(r) or ri["default_branch"]
            git(["fetch", "origin", mr_target], cwd=rp, check=False)
            start = "origin/" + mr_target if have_ref(rp, "origin/" + mr_target) else mr_target
        gr = git(["worktree", "add", "-b", branch, str(wt), start], cwd=rp, check=False)
        if gr.returncode != 0:
            gr2 = git(["worktree", "add", str(wt), branch], cwd=rp, check=False)
            if gr2.returncode != 0:
                return (r, None, None, ((gr.stderr or "") + (gr2.stderr or "")).strip().splitlines()[0][:80] or "worktree add failed")
        for envf in Path(rp).glob(".env*"):
            if envf.name == ".env.example":   # tracked file — copying could create a spurious diff
                continue
            if envf.is_file() and not (Path(wt) / envf.name).exists():
                shutil.copy2(envf, Path(wt) / envf.name)
        setup = ri.get("setup")
        if setup and not no_setup:
            env = dict(os.environ); env["CC_MAIN_REPO"] = rp
            subprocess.run(setup, cwd=str(wt), shell=True, env=env, capture_output=True, text=True)
        return (r, str(wt), mr_target, None)
    except Exception as ex:
        return (r, None, None, str(ex).splitlines()[0][:80])


def cmd_task_add(args):
    s = load_state()
    if args.epic not in s["epics"]:
        die("unknown epic '%s'" % args.epic)
    epic = s["epics"][args.epic]
    proj = s["projects"][epic["project"]]
    repos = args.repos.split(",") if args.repos else (epic.get("repos") or list(proj["repos"].keys()))
    for r in repos:
        if r not in proj["repos"]:
            die("repo '%s' not in project '%s'" % (r, epic["project"]))
    slug = slugify(args.title)
    branch = "%s-%s" % (args.epic, slug)
    # tid is the state key — must be unique. Same slug under a different epic used to
    # COLLIDE (t_<slug>) and silently overwrite; disambiguate by epic, then by number.
    tid = "t_" + slug
    if tid in s["tasks"] and s["tasks"][tid].get("epic") != args.epic:
        tid = "t_" + slugify(args.epic) + "_" + slug
    base_tid, _i = tid, 2
    while tid in s["tasks"]:
        tid = base_tid + "-" + str(_i); _i += 1
    epic_mode = epic.get("mode") or ("targets" if epic.get("targets") else "epic_branch")
    print("task '%s' under epic %s [%s] - provisioning %d repo(s) in parallel ..." % (
        args.title, args.epic, epic_mode, len(repos)))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(repos)))) as exr:
        results = list(exr.map(
            lambda r: _provision(args.epic, epic, proj, r, branch, slug, epic_mode, args.no_setup), repos))
    worktrees, base, skipped, order = {}, {}, [], []
    for (r, wt, tgt, err) in results:
        if err:
            print("  [%s] skipped (%s)" % (r, err)); skipped.append(r); continue
        worktrees[r] = wt; base[r] = tgt; order.append(r)
        print("  [%s] ready -> MR target %s" % (r, tgt))
    if not worktrees:
        die("no usable repos (all skipped). Scope the epic: cc epic set %s --repos <a,b>" % args.epic)
    if skipped:
        print("  (skipped %d: %s)" % (len(skipped), ", ".join(skipped)))
    primary = order[0]
    task_dir = str(Path(worktrees[primary]).parent)   # cctui/<epic>/<slug>/ — repos are its subfolders
    repo_map = "\n".join("- %s -> %s" % (r, worktrees[r]) for r in order)
    epic_mem = (epic.get("memory") or "").strip()
    claude_md = ("# Epic %s: %s\n\n%s\n\n## Repos available for THIS task (branch %s):\n%s\n\n"
                 "Touch ONLY the repos actually relevant to this task; leave the rest UNCHANGED "
                 "(cc opens a Merge Request only for repos you modify, so untouched repos cost nothing). "
                 "Do NOT run git — cc handles branches/commits/MRs.\n") % (
                 args.epic, epic.get("summary", ""), epic_mem or "(no epic notes yet)", branch, repo_map)
    try:
        (Path(task_dir) / "CLAUDE.md").write_text(claude_md)
    except Exception:
        pass
    log = STATE_DIR / ("%s.log" % tid)
    s["tasks"][tid] = {"epic": args.epic, "title": args.title, "prompt": args.prompt,
                       "repos": order, "branch": branch, "worktrees": worktrees, "base": base,
                       "primary": primary, "dir": task_dir, "claude_session": {}, "status": "running",
                       "mrs": {}, "log": str(log)}
    save_state(s)
    full_prompt = (args.prompt
                   + "\n\n[cc] This task spans the repo worktrees below (branch %s). "
                     "Edit files ONLY inside these subfolders (your cwd is their parent):\n%s"
                     "\nDo NOT run git; cc handles branches/commits/MRs." % (branch, repo_map))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.sync:
        print("  running agent (sync, headless) ...")
        rr = subprocess.run(["claude", "--permission-mode", "bypassPermissions", "-p", full_prompt],
                            cwd=task_dir, text=True, capture_output=True)
        log.write_text((rr.stdout or "") + "\n" + (rr.stderr or ""))
        s = load_state(); s["tasks"][tid]["status"] = "review"; save_state(s)
        print("  done.")
    else:
        with open(log, "w") as lf:
            proc = subprocess.Popen(["claude", "--permission-mode", "bypassPermissions", "-p", full_prompt],
                                    cwd=task_dir, stdout=lf, stderr=lf)
        s = load_state(); s["tasks"][tid]["pid"] = proc.pid; s["tasks"][tid].pop("tmux", None); save_state(s)
        print("  agent running in BACKGROUND (pid=%s, cwd=%s) — open the chat with o" % (proc.pid, task_dir))
    jira = proj.get("jira")
    if getattr(args, "jira", None):
        s = load_state(); s["tasks"][tid]["jira"] = args.jira; save_state(s)
        print("  linked to existing jira issue: %s" % args.jira)
        jira = proj.get("jira")
        if jira and jira.get("token") and args.epic.split("-")[0] == jira.get("project_key"):
            try:
                cur = jira_issue_parent(jira, args.jira)
                if cur != args.epic:
                    jira_set_parent(jira, args.jira, args.epic)
                    print("  jira: %s -> moved under epic %s (was %s)" % (
                        args.jira, args.epic, cur or "no parent"))
            except Exception as ex:
                print("  (jira reparent failed: %s)" % str(ex)[:80])
    elif jira and jira.get("token") and not getattr(args, "no_jira", False):
        try:
            jk = jira_create_task(jira, args.epic, args.title, args.prompt)
            if jk:
                s = load_state(); s["tasks"][tid]["jira"] = jk; save_state(s)
                print("  jira task created: %s (under %s)" % (jk, args.epic))
        except Exception as ex:
            print("  (jira task not created: %s)" % str(ex)[:100])
    print("done: cc task open %s   |   cc task diff %s" % (tid, tid))


def _cc_artifact(path):
    """cc's own provisioning leftovers — not the agent's work."""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1] if parts else ""
    return "node_modules" in parts or base == ".cc-task.md" or base.startswith(".env")

def _changed(wt):
    if not os.path.isdir(wt):
        return ""
    out = git(["status", "--short"], cwd=wt, check=False).stdout
    # ignore cc-provisioned artifacts (node_modules symlink, copied .env, task file)
    lines = [l for l in out.splitlines() if l[3:].strip().strip('"') and not _cc_artifact(l[3:].strip().strip('"'))]
    return "\n".join(lines).strip()

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def task_status(t):
    """Derive status from reality: running(bg agent alive) > merged > mr > review > idle."""
    pid = t.get("pid")
    if pid and pid_alive(pid):
        return "running"
    if t.get("merged"):
        return "merged"
    if t.get("mrs"):
        return "mr"
    for r, wt in t.get("worktrees", {}).items():
        if not os.path.isdir(wt):
            continue
        if _changed(wt):
            return "review"
        base = t.get("base", {}).get(r)
        if base:
            cmp_ref = ("origin/" + base) if have_ref(wt, "origin/" + base) else base
            ahead = run(["git", "rev-list", "--count", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout.strip()
            if ahead not in ("", "0"):
                return "review"
    return "idle"

def cmd_task_diff(args):
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    for r in t["repos"]:
        wt = t["worktrees"][r]
        print("\n===== %s  (%s) =====" % (r, wt))
        st = _changed(wt)
        print(st if st else "(no changes)")
        git(["add", "-A", "--intent-to-add", "."], cwd=wt, check=False)
        diff = git(["diff", "--stat"], cwd=wt, check=False).stdout.strip()
        if diff:
            print("--- diffstat ---\n" + diff)

def cmd_task_open(args):
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj = s["projects"][s["epics"][t["epic"]]["project"]]
    proj_name = s["epics"][t["epic"]]["project"]
    task_dir = t.get("dir") or str(Path(next(iter(t["worktrees"].values()))).parent)
    runnable = [(r, proj["repos"].get(r, {}).get("run")) for r in t["repos"]]
    runnable = [(r, rc) for r, rc in runnable if rc]
    # open the task FOLDER directly (repos are subfolders, like Codebase/work/invictus);
    # keep Run Task + sane settings via .vscode/ inside the folder (no .code-workspace)
    vs = Path(task_dir) / ".vscode"
    vs.mkdir(parents=True, exist_ok=True)
    (vs / "settings.json").write_text(json.dumps({
        "window.title": "cc:%s" % args.task,
        "git.repositoryScanMaxDepth": 2,
        "search.exclude": {"**/node_modules": True},
        "files.watcherExclude": {"**/node_modules/**": True},
    }, indent=2))
    if runnable:
        tasks = [{
            "label": "dev: %s" % r, "type": "shell", "command": rc,
            "options": {"cwd": "${workspaceFolder}/%s" % r},
            "isBackground": True, "problemMatcher": [],
            "presentation": {"panel": "dedicated", "group": "ccdev", "reveal": "always"},
        } for r, rc in runnable]
        tasks.append({"label": "cc: dev all", "dependsOrder": "parallel",
                      "dependsOn": ["dev: %s" % r for r, _ in runnable], "problemMatcher": []})
        (vs / "tasks.json").write_text(json.dumps({"version": "2.0.0", "tasks": tasks}, indent=2))
    print("Cursor folder: %s" % task_dir)
    print("  open:   cursor '%s'" % task_dir)
    sid = t.get("claude_session", {}).get(t["primary"])
    if not sid:
        sid = resolve_session(t["worktrees"][t["primary"]])
        if sid:
            t.setdefault("claude_session", {})[t["primary"]] = sid
            save_state(s)
    if sid:
        print("  follow-up:  (cd '%s' && claude --resume %s --permission-mode auto)" % (t["worktrees"][t["primary"]], sid))
    if t.get("log"):
        print("  watch log:  tail -f '%s'" % t["log"])
    if runnable:
        print("  run ALL repos in Cursor:  Cmd+Shift+P -> 'Tasks: Run Task' -> 'cc: dev all'")
        print("    (each repo starts in its own dedicated terminal panel)")
        oneliner = "npx -y concurrently --names %s --prefix-colors auto %s" % (
            ",".join(r for r, _ in runnable),
            " ".join(shlex.quote("cd %s && %s" % (shlex.quote(t["worktrees"][r]), rc))
                     for r, rc in runnable))
        print("  or paste into one Cursor terminal:")
        print("    %s" % oneliner)
    print("  run locally (per repo):")
    for r in t["repos"]:
        rc = proj["repos"].get(r, {}).get("run")
        if not rc:
            rc = "<no dev cmd — set: cc repo set %s %s --run ...>" % (proj_name, r)
        print("    [%s]  cd '%s' && %s" % (r, t["worktrees"][r], rc))

def cmd_task_ls(args):
    s = load_state()
    by_epic = {}
    for tid, t in s["tasks"].items():
        if args.epic and t["epic"] != args.epic:
            continue
        by_epic.setdefault(t["epic"], []).append((tid, t))
    for epic, items in by_epic.items():
        print("[epic] %s  %s" % (epic, s["epics"].get(epic, {}).get("summary", "")))
        for tid, t in items:
            st = task_status(t)
            glyph = {"running": "~", "review": "*", "mr": "MR", "idle": "."}.get(st, "?")
            print("   %s %-18s %-28s [%s]" % (glyph, tid, t["title"], st))

def cmd_task_mrs(args):
    # self-locked: query glab WITHOUT holding the state lock (network is slow), then save briefly.
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj = s["projects"][s["epics"][t["epic"]]["project"]]
    branch = t["branch"]
    any_url = merged = open_ = 0
    found, states = {}, {}
    for r in t["repos"]:
        ri = proj["repos"][r]
        url, state = mr_info(ri["remote"], branch, ri["path"])   # NETWORK, no lock held
        if url:
            found[r] = url
            states[r] = state
            any_url += 1
            print("[%s] %s  (%s)" % (r, url, state))
            if state == "merged":
                merged += 1
            else:
                open_ += 1
        else:
            print("[%s] (no MR)" % r)
    all_merged = any_url > 0 and open_ == 0
    def apply(st):
        tt = st["tasks"].get(args.task)
        if tt is not None:
            tt.setdefault("mrs", {}).update(found)
            tt["mr_state"] = states
            tt["merged"] = all_merged
    mutate(apply)
    if not any_url:
        print("(no MRs found — create with M)")
    elif merged and not open_:
        print("\nall %d MR(s) merged -> `cc task done %s` to clean up worktrees" % (merged, args.task))
    elif merged:
        print("\n%d merged, %d still open" % (merged, open_))


def cmd_task_abort(args):
    """Tear down a task's REMOTE side: close MRs + delete remote branches, then local cleanup.
    Use for throwaway/test tasks so they don't pollute team repos."""
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    epic = s["epics"][t["epic"]]
    proj = s["projects"][epic["project"]]
    branch = t["branch"]
    epic_branch = epic.get("branch") if (epic.get("mode") == "epic_branch") else None
    # is the epic branch still used by OTHER tasks (with MRs)? if so, keep it
    others = [k for k, ot in s["tasks"].items() if k != args.task and ot.get("epic") == t["epic"]]
    drop_epic_branch = epic_branch and not others
    for r in t["repos"]:
        ri = proj["repos"][r]; rp = ri["path"]
        url = (t.get("mrs", {}) or {}).get(r) or find_mr(ri["remote"], branch, rp)
        if url:
            m = re.search(r"/merge_requests/(\d+)", url)
            if m:
                run(["glab", "mr", "close", m.group(1), "-R", ri["remote"]], cwd=rp, check=False)
                print("[%s] closed MR !%s" % (r, m.group(1)))
        if run(["git", "push", "origin", "--delete", branch], cwd=rp, check=False).returncode == 0:
            print("[%s] deleted remote branch %s" % (r, branch))
        if drop_epic_branch:
            if run(["git", "push", "origin", "--delete", epic_branch], cwd=rp, check=False).returncode == 0:
                print("[%s] deleted remote epic branch %s" % (r, epic_branch))
    # local cleanup
    if t.get("pid") and pid_alive(t["pid"]):
        try:
            os.kill(int(t["pid"]), 15)
        except Exception:
            pass
    task_dir = Path(next(iter(t["worktrees"].values()))).parent if t.get("worktrees") else None
    for r in t["repos"]:
        rp = proj["repos"].get(r, {}).get("path")
        if rp:
            git(["worktree", "remove", t["worktrees"][r], "--force"], cwd=rp, check=False)
            git(["worktree", "prune"], cwd=rp, check=False)
            git(["branch", "-D", branch], cwd=rp, check=False)
    if task_dir and task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)
    s["tasks"].pop(args.task, None)
    save_state(s)
    print("task %s aborted (MRs closed, remote+local branches/worktrees removed)." % args.task)


def cmd_task_done(args):
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj = s["projects"][s["epics"][t["epic"]]["project"]]
    problems = [("%s has uncommitted changes" % r) for r in t["repos"] if _changed(t["worktrees"][r])]
    if problems and not args.force:
        die("refusing cleanup:\n  - " + "\n  - ".join(problems) + "\n(use --force to override)")
    if t.get("pid") and pid_alive(t["pid"]):
        try:
            os.kill(int(t["pid"]), 15)
        except Exception:
            pass
    task_dir = Path(next(iter(t["worktrees"].values()))).parent
    for r in t["repos"]:
        rp = proj["repos"].get(r, {}).get("path")
        if not rp:
            continue
        git(["worktree", "remove", t["worktrees"][r], "--force"], cwd=rp, check=False)
        git(["worktree", "prune"], cwd=rp, check=False)
        print("removed worktree: %s" % t["worktrees"][r])
    if task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)
        print("removed task dir: %s" % task_dir)
    t["status"] = "done"
    save_state(s)
    print("task %s archived." % args.task)

def find_mr(remote, branch, cwd):
    """Web URL of the MR for this branch (or None) — looks up existing via glab mr list."""
    lst = run(["glab", "mr", "list", "-R", remote, "--source-branch", branch], cwd=cwd, check=False)
    text = (lst.stdout or "") + (lst.stderr or "")
    m = re.search(r"https?://\S+/-/merge_requests/\d+", text)
    if m:
        return m.group(0)
    m = re.search(r"!(\d+)", text)
    if m:
        return "https://gitlab.com/%s/-/merge_requests/%s" % (remote, m.group(1))
    return None

def mr_info(remote, branch, cwd):
    """(url, state) for the MR of this branch via `glab mr view`, or (None, None)."""
    r = run(["glab", "mr", "view", branch, "-R", remote], cwd=cwd, check=False)
    if r.returncode != 0:
        return (None, None)
    text = r.stdout or ""
    um = re.search(r"url:\s*(\S+)", text)
    sm = re.search(r"state:\s*(\w+)", text)
    return (um.group(1) if um else find_mr(remote, branch, cwd), sm.group(1) if sm else "?")

def mr_url(out, remote, branch, cwd):
    text = (out.stdout or "") + "\n" + (out.stderr or "")
    m = re.search(r"https?://\S+/-/merge_requests/\d+", text)   # real web url from create
    if m:
        return m.group(0)
    return find_mr(remote, branch, cwd) or "(MR exists — see `cc task mrs`)"


def claude_text(cwd, prompt, timeout=120):
    """Generate text via headless claude in cwd (loads that dir's CLAUDE.md). None on failure."""
    if not shutil.which("claude"):
        return None
    try:
        r = subprocess.run(["claude", "-p", "--permission-mode", "bypassPermissions", prompt],
                           cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out or None
    except Exception:
        return None

def gen_commit_msg(wt, fallback):
    diff = git(["diff", "--cached"], cwd=wt, check=False).stdout or ""
    if not diff.strip():
        return fallback
    prompt = ("Write ONE git commit message for the staged diff below, following this repo's "
              "conventions in CLAUDE.md (if absent, use Conventional Commits: type(scope): subject). "
              "Output ONLY the message: a subject line, optionally a blank line then a short body. "
              "No backticks, no quotes, no preamble.\n\n=== staged diff ===\n" + diff[:12000])
    msg = claude_text(wt, prompt)
    return msg.strip() if msg else fallback

def gen_mr_text(wt, cmp_ref, fallback_title, fallback_desc):
    diff = git(["diff", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout or ""
    log = git(["log", "--oneline", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout or ""
    if not diff.strip():
        return (fallback_title, fallback_desc)
    prompt = ("Summarize this branch into a Merge Request, following the repo's CLAUDE.md conventions. "
              "Output the MR title on the first line, then a line containing only '---', then the MR "
              "description in markdown (what changed and why; concise, factual). No preamble.\n\n"
              "=== commits ===\n" + log[:2000] + "\n\n=== diff ===\n" + diff[:14000])
    out = claude_text(wt, prompt)
    if not out:
        return (fallback_title, fallback_desc)
    if "---" in out:
        a, b = out.split("---", 1)
        title = (a.strip().splitlines()[0][:140] if a.strip() else fallback_title)
        return (title, b.strip() or fallback_desc)
    lines = out.strip().splitlines()
    return (lines[0][:140], "\n".join(lines[1:]).strip() or fallback_desc)


def cmd_task_mr(args):
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    epic = s["epics"][t["epic"]]
    proj = s["projects"][epic["project"]]
    epic_mode = epic.get("mode") or ("targets" if epic.get("targets") else "epic_branch")
    assignee = proj.get("default_assignee") or glab_user()
    any_real = False
    for r in t["repos"]:
        wt = t["worktrees"][r]
        ri = proj["repos"][r]
        target = t["base"][r]
        branch = t["branch"]
        cmp_ref = ("origin/" + target) if have_ref(wt, "origin/" + target) else target
        ahead = run(["git", "rev-list", "--count", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout.strip()
        if not _changed(wt) and ahead in ("", "0"):
            print("[%s] no changes - skipped (no branch pushed, no MR)" % r)
            continue
        lead = ri.get("reviewer", "")
        title = "[%s] %s" % (t["epic"], t["title"])
        push = ["git", "push", "--no-verify", "-u", "origin", branch]
        if args.dry_run:
            print("[%s] DRY-RUN: %s -> %s | title=%r reviewer=%s assignee=%s label=%s" % (
                r, branch, target, title, lead or "-", assignee or "-", t["epic"]))
            continue
        any_real = True
        print("[%s] preparing %s ..." % (r, branch))
        if epic_mode == "epic_branch":
            print("[%s] pushing epic branch %s ..." % (r, target))
            push_epic_branch(proj["repos"][r]["path"], target)
        if _changed(wt):
            git(["add", "-A"], cwd=wt)
            git(["reset", "-q", "--", "node_modules", ".env", ".env.local", ".env.development",
                 ".env.example", ".cc-task.md"], cwd=wt, check=False)
            if getattr(args, "no_ai", False):
                msg = title
            else:
                print("[%s] commit-сообщение через claude (CLAUDE.md репо) ..." % r)
                msg = gen_commit_msg(wt, title)
            git(["commit", "--no-verify", "-m", msg], cwd=wt)
            print("[%s] commit: %s" % (r, (msg or title).splitlines()[0][:80]))
        print("[%s] push %s ..." % (r, branch))
        run(push, cwd=wt)
        existing = find_mr(ri["remote"], branch, wt)
        if existing:
            t["mrs"][r] = existing
            print("[%s] MR уже есть -> запушены новые коммиты, обновлён: %s" % (r, existing))
            continue
        diffstat = run(["git", "diff", "--stat", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout.strip()
        fb_desc = (t.get("prompt") or "").strip()
        if diffstat:
            fb_desc += "\n\n## Changes\n```\n" + diffstat + "\n```"
        if getattr(args, "no_ai", False):
            mr_title, mr_desc = title, fb_desc
        else:
            print("[%s] MR-текст через claude (CLAUDE.md репо) ..." % r)
            ai_title, mr_desc = gen_mr_text(wt, cmp_ref, t["title"], fb_desc)
            mr_title = "[%s] %s" % (t["epic"], ai_title)
        mr_desc += "\n\n_MR by cc — epic %s_" % t["epic"]
        glab_cmd = ["glab", "mr", "create", "-R", ri["remote"],
                    "--source-branch", branch, "--target-branch", target,
                    "--title", mr_title, "--description", mr_desc[:8000],
                    "--label", t["epic"], "--yes"]
        if assignee:
            glab_cmd += ["--assignee", assignee]
        if lead:
            glab_cmd += ["--reviewer", lead]
        print("[%s] glab mr create -> %s (reviewer %s, assignee %s) ..." % (r, target, lead or "-", assignee or "-"))
        out = run(glab_cmd, cwd=wt, check=False)
        url = mr_url(out, ri["remote"], branch, wt)
        t["mrs"][r] = url
        print("[%s] MR -> %s" % (r, url))
    if any_real:
        t["status"] = "mr"
        save_state(s)

def _epic_branch_map(e, proj):
    """repo -> the integration/epic branch whose merge to master represents this epic. In targets
    mode that's each repo's integration branch; in epic-branch mode the single epic branch (only
    for repos that actually have it)."""
    mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
    if mode == "targets":
        return {r: br for r, br in (e.get("targets") or {}).items() if r in proj["repos"]}
    key = e.get("branch")
    if not key:
        return {}
    return {r: key for r, ri in proj["repos"].items()
            if have_ref(ri["path"], key) or have_ref(ri["path"], "origin/" + key)}

def _epic_master_mr(ri, br, epic_key):
    """The MR (any state, incl. merged) that brings this epic into the repo's default branch.
    Matches an MR targeting main/master whose source branch is the integration branch OR starts
    with the epic key (covers a dedicated release branch like '<KEY>/website-prod-release').
    Returns (url, state, default_branch)."""
    rp, remote = ri["path"], ri["remote"]
    db = default_branch(rp)
    enc = remote.replace("/", "%2F")
    out = run(["glab", "api",
               "projects/%s/merge_requests?target_branch=%s&state=all&per_page=50" % (enc, db)],
              cwd=rp, check=False)
    try:
        arr = json.loads(out.stdout or "[]")
    except Exception:
        arr = []
    p1, p2 = epic_key + "/", epic_key + "-"
    def hit(mr):
        sb = mr.get("source_branch", "") or ""
        return sb == br or sb == epic_key or sb.startswith(p1) or sb.startswith(p2)
    matches = [mr for mr in arr if hit(mr)]
    if not matches:
        return (None, None, db)
    opened = [m for m in matches if m.get("state") == "opened"]
    pick = (opened or matches)[0]   # prefer an open MR, else the most-recent match
    return (pick.get("web_url"), pick.get("state"), db)

def _epic_sync_mrs(e, proj, epic_key):
    """NETWORK ONLY — query GitLab for each repo's epic->master MR. Returns (found, states); the
    caller persists. Deliberately does NOT hold the state lock: the glab calls take seconds and we
    don't want to block other cc mutations (or the TUI) while we wait on the network."""
    found, states = {}, {}
    for r, br in _epic_branch_map(e, proj).items():
        ri = proj["repos"][r]
        url, state, db = _epic_master_mr(ri, br, epic_key)
        if url:
            found[r] = url
            states[r] = state or "?"
            print("[%s] -> %s  %s  (%s)" % (r, db, url, state))
        else:
            print("[%s] MR в %s не найден (ветка %s)" % (r, db, br))
    return found, states

def cmd_epic_mr(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
    if mode == "targets":
        # integration branches are shared/team-owned; don't auto-create their release MRs —
        # just detect & record the existing target-branch -> master MRs for display.
        print("targets mode: интеграционные ветки релизятся своим потоком; ищу существующие MR → master …")
        found, states = _epic_sync_mrs(e, proj, args.key)
        e.setdefault("mrs", {}).update(found)
        e["mr_state"] = states
        save_state(s)   # lock already held by main() for cmd_epic_mr
        if not found:
            print("(MR интеграционных веток → master пока нет)")
        return
    key = e.get("branch", args.key)
    found = False
    for r, ri in proj["repos"].items():
        rp = ri["path"]
        if not (have_ref(rp, key) or have_ref(rp, "origin/" + key)):
            continue
        found = True
        db = default_branch(rp)
        lead = ri.get("reviewer", "")
        assignee = proj.get("default_assignee") or glab_user()
        glab_cmd = ["glab", "mr", "create", "-R", ri["remote"],
                    "--source-branch", key, "--target-branch", db,
                    "--title", "[%s] %s" % (args.key, e.get("summary", "") or "epic integration"),
                    "--description", "Epic %s integration -> %s\n\n_MR by cc_" % (args.key, db),
                    "--label", args.key, "--yes"]
        if assignee:
            glab_cmd += ["--assignee", assignee]
        if lead:
            glab_cmd += ["--reviewer", lead]
        if args.dry_run:
            print("[%s] DRY-RUN epic MR: %s -> %s (reviewer %s)" % (r, key, db, lead or "-"))
            print("    would: git push -u origin %s" % key)
            print("    would: " + " ".join(glab_cmd))
            continue
        push_epic_branch(rp, key)
        out = run(glab_cmd, cwd=rp, check=False)
        url = mr_url(out, ri["remote"], key, rp)
        e.setdefault("mrs", {})[r] = url
        print("[%s] epic MR -> %s" % (r, url))
    if found:
        save_state(s)
    else:
        print("(no epic branch '%s' in any repo yet - create a task first)" % key)


def cmd_epic_note(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    e["memory"] = ((e.get("memory", "") or "").rstrip() + "\n- " + args.text).strip()
    save_state(s)
    print("noted on %s (memory now %d lines)" % (args.key, e["memory"].count("\n") + 1))

def cmd_epic_memory(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    print(e.get("memory", "") or "(empty — `cc epic note %s \"...\"`)" % args.key)


def cmd_epic_mrs(args):
    # self-locked: runs WITHOUT main()'s global lock so the slow glab calls don't block other cc
    # mutations / the TUI; only the brief save below takes the lock.
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    found, states = _epic_sync_mrs(e, proj, args.key)   # NETWORK, no lock held
    def apply(st):
        ep = st["epics"].get(args.key)
        if ep is not None:
            ep.setdefault("mrs", {}).update(found)
            ep["mr_state"] = states
    mutate(apply)
    if not found:
        mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
        print("(MR ветки эпика ещё нет — нажми M / cc epic mr %s)" % args.key if mode == "epic_branch"
              else "(MR интеграционных веток → master пока нет)")


def sync_epic_children(s, key):
    """Cache the epic's Jira child issues into epic['jira_children']. Returns count or -1."""
    e = s["epics"].get(key)
    if not e:
        return -1
    jira = s["projects"][e["project"]].get("jira")
    if not (jira and jira.get("token") and key.split("-")[0] == jira.get("project_key")):
        return -1
    try:
        e["jira_children"] = jira_epic_children(jira, key, limit=100)
        return len(e["jira_children"])
    except Exception:
        return -1

def cmd_epic_sync(args):
    s = load_state()
    if args.key not in s["epics"]:
        die("unknown epic '%s'" % args.key)
    n = sync_epic_children(s, args.key)
    if n < 0:
        die("epic %s is not a Jira epic (or Jira not configured)" % args.key)
    save_state(s)
    print("epic %s: cached %d Jira child issue(s)" % (args.key, n))


def cmd_epic_archive(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    e["archived"] = True
    save_state(s)
    print("epic %s archived (под 'Архив'; `cc epic unarchive %s` чтобы вернуть)" % (args.key, args.key))
    # push the whole epic to Done in Jira: the epic issue + all its children (best effort)
    proj = s["projects"][e["project"]]
    jira = proj.get("jira")
    if not (jira and jira.get("token") and args.key.split("-")[0] == jira.get("project_key")):
        return
    try:
        children = [c["key"] for c in jira_epic_children(jira, args.key)]
    except Exception:
        children = []
    keys = [args.key] + children
    print("  jira: перевожу в Done %d issue(s) …" % len(keys))
    done = skipped = failed = 0
    for k in keys:
        if jira_status_category(jira, k) == "done":
            print("    %-10s уже Done" % k); skipped += 1; continue
        ok, info = jira_transition_done(jira, k)
        if ok:
            print("    %-10s -> Done (%s)" % (k, info)); done += 1
        else:
            print("    %-10s не смог: %s" % (k, info)); failed += 1
    print("  jira итог: %d переведено, %d уже Done, %d не смог" % (done, skipped, failed))

def cmd_epic_unarchive(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    e.pop("archived", None)
    save_state(s)
    print("epic %s unarchived" % args.key)

def cmd_epic_rm(args):
    s = load_state()
    if args.key not in s["epics"]:
        die("unknown epic '%s'" % args.key)
    proj = s["projects"][s["epics"][args.key]["project"]]
    tasks = [t for t, v in s["tasks"].items() if v.get("epic") == args.key]
    if tasks and not args.force:
        die("epic %s has %d task(s): %s\n"
            "Use --force to delete the epic AND its tasks (local worktrees removed). "
            "Remote MRs/branches are left intact — run `cc task abort <task>` first if you want those gone."
            % (args.key, len(tasks), ", ".join(tasks)))
    # --force: tear down each task's LOCAL worktrees + dirs (remote side untouched)
    for tid in tasks:
        t = s["tasks"][tid]
        dirty = [r for r in t.get("repos", []) if _changed(t.get("worktrees", {}).get(r, ""))]
        if dirty:
            print("  ! task %s has uncommitted changes in: %s (removing anyway)" % (tid, ", ".join(dirty)))
        task_dir = Path(next(iter(t["worktrees"].values()))).parent if t.get("worktrees") else None
        sess = t.get("tmux")
        if sess:
            subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        for r in t.get("repos", []):
            rp = proj["repos"].get(r, {}).get("path")
            wt = t.get("worktrees", {}).get(r)
            if rp and wt:
                git(["worktree", "remove", wt, "--force"], cwd=rp, check=False)
                git(["worktree", "prune"], cwd=rp, check=False)
                git(["branch", "-D", t["branch"]], cwd=rp, check=False)
        if task_dir and task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        s["tasks"].pop(tid, None)
        print("  removed task %s (worktrees + dir)" % tid)
    # remove the epic's cctui dir if it's now empty (never touch sibling epics)
    epic_dir = Path(proj.get("path", "")) / "cctui" / args.key
    try:
        if epic_dir.exists() and not any(epic_dir.iterdir()):
            epic_dir.rmdir()
    except Exception:
        pass
    s["epics"].pop(args.key, None)
    save_state(s)
    print("epic %s removed from cc%s (Jira + remote MRs/branches NOT touched)" % (
        args.key, (" + %d task(s) incl. their worktrees" % len(tasks)) if tasks else ""))


RELEASE_RUNBOOK = """# Epic %s: %s — release & coordination chat

You drive RELEASES and cross-repo coordination for THIS epic. The repos are added with
--add-dir (their MAIN checkouts). cc does NOT own git here — you run git/glab/eas yourself.

## Repos & integration branches (release source / MR target)
%s

## Epic notes
%s

## Release runbook (when asked to release this epic)
Per repo (NEVER disturb the user's working checkout — use a temp `git worktree` off the
integration branch for the version bump):
1. Confirm the integration branch is green on stage (CI) before releasing.
2. Bump version (package.json / equivalent) on a release branch off the integration branch.
3. MR integration -> main (reviewer = area lead); merge once the pipeline is green.
4. Tag vX.Y.Z on main -> the tag pipeline runs the prod jobs.
5. Play/await `Containerize Prod` + `Deploy ECS Prod` (GitLab), or `eas update --branch production` (Expo/mobile).
6. New website routes (e.g. /loyalty): POST build-website-routes so they register on prod.
7. Close the epic's Jira tasks + the epic -> Done.

## Release train & safety (read before prod)
- Backend has no API versioning: web/admin must not hit prod before backend+mobile. Release
  the train together; verify the backend prod actually has the endpoints the frontend calls.
- Feature-flag-gated surfaces ship DARK; flip the flag only after the backend is live on prod.
- glab: use canonical `invictusfitness/*` paths (moved forks return 405 on POST). eas for mobile.
- Full autonomy through prod is authorized for this chat — still narrate every step and the result.
"""

def cmd_epic_open(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    repos = e.get("repos") or list(proj["repos"].keys())
    targets = e.get("targets") or {}
    edir = Path(proj["path"]) / "cctui" / args.key / "_release"
    edir.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in repos:
        ri = proj["repos"].get(r, {})
        lines.append("- %s: integration `%s`  (checkout: %s, reviewer: %s)" % (
            r, targets.get(r) or "(default branch)", ri.get("path", ""), ri.get("reviewer") or "-"))
    (edir / "CLAUDE.md").write_text(
        RELEASE_RUNBOOK % (args.key, e.get("summary", ""), "\n".join(lines),
                           (e.get("memory") or "").strip() or "(no epic notes)"))
    adds = " ".join("--add-dir %s" % shlex.quote(proj["repos"][r]["path"])
                    for r in repos if proj["repos"].get(r, {}).get("path"))
    print("epic chat dir: %s" % edir)
    print("  release runbook CLAUDE.md written (%d repo(s))" % len(repos))
    print("  open:  cd %s && claude --permission-mode auto %s" % (shlex.quote(str(edir)), adds))


def cmd_epic_set(args):
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    if args.summary is not None:
        e["summary"] = args.summary
    proj = s["projects"][e["project"]]
    if args.repos is not None:
        rl = [x for x in args.repos.split(",") if x]
        for r in rl:
            if r not in proj["repos"]:
                die("repo '%s' not in project '%s'" % (r, e["project"]))
        e["repos"] = rl or None
    if args.target:
        targets = dict(e.get("targets") or {})
        for pair in args.target:
            if "=" not in pair:
                die("--target must be repo=branch, got '%s'" % pair)
            rr, br = pair.split("=", 1)
            if rr not in proj["repos"]:
                die("repo '%s' not in project '%s'" % (rr, e["project"]))
            targets[rr] = br
        e["targets"] = targets
        e["mode"] = "targets"   # route MRs to these branches; no bare epic branch (avoids D/F key clashes)
    save_state(s)
    print("epic %s  mode=%s  repos=%s  targets=%s  summary=%r" % (
        args.key, e.get("mode") or ("targets" if e.get("targets") else "epic_branch"),
        e.get("repos") or "ALL", e.get("targets") or "-", e.get("summary", "")))

def cmd_repo_members(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    ri = proj["repos"].get(args.repo) or die("unknown repo '%s'" % args.repo)
    for m in glab_members(ri["remote"]):
        print("%-26s L%-3d %s" % (m["username"], m["access"], m["name"]))


def cmd_repo_set(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    r = proj["repos"].get(args.repo) or die("unknown repo '%s'" % args.repo)
    if args.setup is not None:
        r["setup"] = args.setup
    if args.run is not None:
        r["run"] = args.run
    if args.reviewer is not None:
        r["reviewer"] = args.reviewer
    save_state(s)
    print("%s/%s  setup=%r  run=%r  reviewer=%r" % (args.project, args.repo, r.get("setup", ""), r.get("run", ""), r.get("reviewer", "")))

def cmd_repo_ls(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    for rn, ri in proj["repos"].items():
        print("%-22s setup=%r  run=%r" % (rn, ri.get("setup", ""), ri.get("run", "")))


# ----------------------------- jira -----------------------------

def jira_req(cfg, method, path, body=None):
    url = "https://%s/rest/api/3%s" % (cfg["site"], path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    auth = base64.b64encode(("%s:%s" % (cfg["email"], cfg["token"])).encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}

def jira_my_epics(cfg, query=""):
    # all project epics, most-recently-updated first (assignee filter dropped: leads often
    # aren't the assignee of the epics they pick up). Search narrows by summary.
    jql = "project = %s AND issuetype = Epic" % cfg["project_key"]
    if query:
        jql += ' AND summary ~ "%s"' % query.replace('"', "")
    jql += " ORDER BY updated DESC"
    body = {"jql": jql, "maxResults": 50, "fields": ["summary", "status"]}
    try:
        data = jira_req(cfg, "POST", "/search/jql", body)
    except Exception:
        data = jira_req(cfg, "POST", "/search", body)
    out = []
    for i in data.get("issues", []):
        f = i.get("fields", {})
        st = (f.get("status") or {}).get("name", "")
        out.append({"key": i["key"], "summary": f.get("summary", ""), "status": st})
    return out

def jira_epic_children(cfg, epic_key, query="", limit=50):
    """Issues whose parent is this epic (team-managed). [{key,summary,status,done}]."""
    jql = "parent = %s" % epic_key
    if query:
        jql += ' AND summary ~ "%s"' % query.replace('"', "")
    jql += " ORDER BY created DESC"
    body = {"jql": jql, "maxResults": limit, "fields": ["summary", "status"]}
    try:
        data = jira_req(cfg, "POST", "/search/jql", body)
    except Exception:
        data = jira_req(cfg, "POST", "/search", body)
    out = []
    for i in data.get("issues", []):
        f = i.get("fields", {})
        st = f.get("status") or {}
        out.append({"key": i["key"], "summary": f.get("summary", ""),
                    "status": st.get("name", ""),
                    "done": ((st.get("statusCategory") or {}).get("key") or "").lower() == "done"})
    return out

def jira_orphan_tasks(cfg, query="", limit=30):
    """Project tasks with NO parent epic (candidates to move under an epic). [{key,summary,status,done,orphan}]."""
    jql = "project = %s AND issuetype != Epic AND parent is EMPTY" % cfg["project_key"]
    if query:
        jql += ' AND summary ~ "%s"' % query.replace('"', "")
    jql += " ORDER BY created DESC"
    body = {"jql": jql, "maxResults": limit, "fields": ["summary", "status"]}
    try:
        data = jira_req(cfg, "POST", "/search/jql", body)
    except Exception:
        data = jira_req(cfg, "POST", "/search", body)
    out = []
    for i in data.get("issues", []):
        f = i.get("fields", {})
        st = f.get("status") or {}
        out.append({"key": i["key"], "summary": f.get("summary", ""),
                    "status": st.get("name", ""),
                    "done": ((st.get("statusCategory") or {}).get("key") or "").lower() == "done",
                    "orphan": True})
    return out

def jira_issue_parent(cfg, key):
    try:
        f = jira_req(cfg, "GET", "/issue/%s?fields=parent" % key).get("fields", {})
        return (f.get("parent") or {}).get("key")
    except Exception:
        return None

def jira_set_parent(cfg, key, epic_key):
    jira_req(cfg, "PUT", "/issue/%s" % key, {"fields": {"parent": {"key": epic_key}}})

def jira_status_category(cfg, key):
    try:
        f = jira_req(cfg, "GET", "/issue/%s?fields=status" % key).get("fields", {})
        return (((f.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
    except Exception:
        return ""

def jira_transition_done(cfg, key):
    """Transition an issue to a status in the 'done' category. (ok, info)."""
    try:
        trs = jira_req(cfg, "GET", "/issue/%s/transitions" % key).get("transitions", [])
    except Exception as e:
        return (False, "transitions fetch failed: %s" % str(e)[:50])
    t = next((x for x in trs if (((x.get("to") or {}).get("statusCategory") or {}).get("key") or "").lower() == "done"), None)
    if not t:
        return (False, "no Done transition from current status")
    try:
        jira_req(cfg, "POST", "/issue/%s/transitions" % key, {"transition": {"id": t["id"]}})
        return (True, (t.get("to") or {}).get("name", "Done"))
    except Exception as e:
        return (False, str(e)[:50])

def jira_adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": (text or " ")[:4000]}]}]}

def jira_discover(cfg):
    """Fill account_id + epic/task issuetype ids (best effort). Mutates cfg."""
    try:
        cfg["account_id"] = jira_req(cfg, "GET", "/myself").get("accountId", "")
    except Exception:
        pass
    try:
        data = jira_req(cfg, "GET", "/issue/createmeta/%s/issuetypes" % cfg["project_key"])
        for it in (data.get("issueTypes") or data.get("values") or []):
            if it.get("hierarchyLevel") == 1 and not cfg.get("epic_type_id"):
                cfg["epic_type_id"] = it["id"]
            if it.get("hierarchyLevel") == 0 and not it.get("subtask") and not cfg.get("task_type_id"):
                cfg["task_type_id"] = it["id"]
    except Exception:
        pass

def jira_epic_description(cfg, key):
    try:
        f = jira_req(cfg, "GET", "/issue/%s?fields=description" % key).get("fields", {})
        d = f.get("description")
        if isinstance(d, str):
            return d
        # ADF -> plain text (best effort)
        out = []
        def walk(n):
            if isinstance(n, dict):
                if n.get("type") == "text":
                    out.append(n.get("text", ""))
                for c in n.get("content", []) or []:
                    walk(c)
                if n.get("type") == "paragraph":
                    out.append("\n")
        walk(d or {})
        return "".join(out).strip()
    except Exception:
        return ""

def jira_create_epic(cfg, summary, description=""):
    fields = {"project": {"key": cfg["project_key"]},
              "issuetype": {"id": cfg.get("epic_type_id", "10000")}, "summary": summary}
    if cfg.get("account_id"):
        fields["assignee"] = {"accountId": cfg["account_id"]}
    if description:
        fields["description"] = jira_adf(description)
    return jira_req(cfg, "POST", "/issue", {"fields": fields}).get("key")

def jira_create_task(cfg, epic_key, summary, description=""):
    fields = {"project": {"key": cfg["project_key"]},
              "issuetype": {"id": cfg.get("task_type_id", "10001")},
              "summary": summary, "parent": {"key": epic_key}}
    if cfg.get("account_id"):
        fields["assignee"] = {"accountId": cfg["account_id"]}
    if description:
        fields["description"] = jira_adf(description)
    return jira_req(cfg, "POST", "/issue", {"fields": fields}).get("key")

def cmd_project_jira(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    j = proj.setdefault("jira", {})
    if args.site:
        j["site"] = args.site.replace("https://", "").replace("http://", "").rstrip("/")
    if args.email:
        j["email"] = args.email
    if args.token:
        j["token"] = args.token
    if args.project_key:
        j["project_key"] = args.project_key
    if args.off:
        proj.pop("jira", None)
        save_state(s); print("jira disabled for '%s'" % args.project); return
    save_state(s)
    print("jira for '%s': site=%s email=%s project=%s token=%s" % (
        args.project, j.get("site", "-"), j.get("email", "-"), j.get("project_key", "-"),
        "set" if j.get("token") else "MISSING"))
    if j.get("site") and j.get("token") and j.get("project_key"):
        try:
            n = len(jira_my_epics(j))
            jira_discover(j); save_state(s)
            print("  ok — auth works, %d of your epics in %s (epic-type=%s task-type=%s)" % (
                n, j["project_key"], j.get("epic_type_id", "?"), j.get("task_type_id", "?")))
        except Exception as ex:
            print("  WARN — could not reach Jira: %s" % str(ex)[:120])

def cmd_jira_create_epic(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    cfg = proj.get("jira") or die("Jira not configured for '%s'" % args.project)
    if not cfg.get("epic_type_id"):
        jira_discover(cfg); save_state(s)
    key = jira_create_epic(cfg, args.summary)
    print("created Jira epic: %s  %s" % (key, args.summary))


def cmd_jira_epics(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    cfg = proj.get("jira") or die("Jira not configured — `cc project jira %s --site .. --email .. --token .. --project-key ..`" % args.project)
    eps = jira_my_epics(cfg, args.search or "")
    for e in eps:
        print("%-10s %-8s %s" % (e["key"], e.get("status", ""), e["summary"]))
    if not eps:
        print("(no epics found for you in %s)" % cfg["project_key"])


# ----------------------------- deploys -----------------------------

def repo_deploy_state(ri):
    """What's deployed per env. GitLab repos -> dev/stage/prod (ref@sha); Expo repos -> EAS staging."""
    rp = ri.get("path", "")
    if os.path.exists(os.path.join(rp, "eas.json")) or os.path.exists(os.path.join(rp, "app.config.js")):
        st = {"kind": "eas", "channels": {}}
        for br in ("staging", "production"):
            r = run(["eas", "update:list", "--branch", br, "--limit", "1", "--json", "--non-interactive"],
                    cwd=rp, check=False)
            try:
                out = r.stdout or ""
                data = json.loads(out[out.index("{"):])  # eas prints an upgrade banner before the JSON
                cp = data.get("currentPage") or []
                msg = (cp[0].get("message") or "").strip().strip('"') if cp else ""
                st["channels"][br] = msg[:22] or "(no updates)"
            except Exception:
                st["channels"][br] = "?"
        return st
    enc = (ri.get("remote") or "").replace("/", "%2F")
    st = {"kind": "gitlab", "envs": {}}
    for env in ("dev", "stage", "prod"):
        r = run(["glab", "api",
                 "projects/%s/deployments?environment=%s&status=success&sort=desc&per_page=1" % (enc, env)],
                check=False)
        try:
            arr = json.loads(r.stdout)
            if isinstance(arr, list) and arr:
                dp = arr[0].get("deployable") or {}
                ref = dp.get("ref", "?")
                # a deployment from an MR pipeline has ref refs/merge-requests/N/head —
                # resolve it to that MR's source branch (what's actually deployed)
                m = re.match(r"refs/merge-requests/(\d+)/head", ref or "")
                if m:
                    mrr = run(["glab", "api", "projects/%s/merge_requests/%s" % (enc, m.group(1))], check=False)
                    try:
                        sb = json.loads(mrr.stdout).get("source_branch")
                        if sb:
                            ref = sb
                    except Exception:
                        pass
                st["envs"][env] = {"ref": ref,
                                   "sha": ((dp.get("commit") or {}).get("short_id") or "")[:8],
                                   "at": (arr[0].get("created_at") or "")[:10]}
        except Exception:
            pass
    return st

def cmd_deploys(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    repos = [args.repo] if args.repo else list(proj["repos"].keys())
    for r in repos:
        ri = proj["repos"].get(r)
        if not ri:
            continue
        st = repo_deploy_state(ri)
        ri["deploy"] = st
        if st["kind"] == "eas":
            ch = st.get("channels", {})
            print("%-22s EAS staging=%s | prod=%s" % (r, ch.get("staging", "?"), ch.get("production", "?")))
        else:
            envs = st.get("envs", {})
            parts = ["%s=%s@%s" % (e, envs[e]["ref"], envs[e]["sha"]) for e in ("dev", "stage", "prod") if e in envs]
            print("%-22s %s" % (r, "  ".join(parts) or "(no deployments)"))
    save_state(s)


# ----------------------------- cli -----------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="cc", description="multi-repo epic-routed agent orchestrator")
    sub = p.add_subparsers(dest="group", required=True)

    pj = sub.add_parser("project").add_subparsers(dest="cmd", required=True)
    a = pj.add_parser("add"); a.add_argument("path"); a.add_argument("name", nargs="?"); a.set_defaults(fn=cmd_project_add)
    pj.add_parser("ls").set_defaults(fn=cmd_project_ls)
    a = pj.add_parser("jira"); a.add_argument("project")
    a.add_argument("--site"); a.add_argument("--email"); a.add_argument("--token")
    a.add_argument("--project-key", dest="project_key"); a.add_argument("--off", action="store_true")
    a.set_defaults(fn=cmd_project_jira)

    a = sub.add_parser("deploys"); a.add_argument("project"); a.add_argument("repo", nargs="?")
    a.set_defaults(fn=cmd_deploys)

    jr = sub.add_parser("jira").add_subparsers(dest="cmd", required=True)
    a = jr.add_parser("epics"); a.add_argument("project"); a.add_argument("--search")
    a.set_defaults(fn=cmd_jira_epics)
    a = jr.add_parser("create-epic"); a.add_argument("project"); a.add_argument("summary")
    a.set_defaults(fn=cmd_jira_create_epic)

    rp_ = sub.add_parser("repo").add_subparsers(dest="cmd", required=True)
    a = rp_.add_parser("set"); a.add_argument("project"); a.add_argument("repo")
    a.add_argument("--setup"); a.add_argument("--run"); a.add_argument("--reviewer"); a.set_defaults(fn=cmd_repo_set)
    a = rp_.add_parser("ls"); a.add_argument("project"); a.set_defaults(fn=cmd_repo_ls)
    a = rp_.add_parser("members"); a.add_argument("project"); a.add_argument("repo"); a.set_defaults(fn=cmd_repo_members)

    ep = sub.add_parser("epic").add_subparsers(dest="cmd", required=True)
    a = ep.add_parser("add"); a.add_argument("project"); a.add_argument("key")
    a.add_argument("--summary"); a.add_argument("--target", action="append"); a.add_argument("--repos")
    a.set_defaults(fn=cmd_epic_add)
    a = ep.add_parser("ls"); a.add_argument("project", nargs="?"); a.set_defaults(fn=cmd_epic_ls)
    a = ep.add_parser("mr"); a.add_argument("key"); a.add_argument("--dry-run", action="store_true"); a.set_defaults(fn=cmd_epic_mr)
    a = ep.add_parser("mrs"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_mrs)
    a = ep.add_parser("set"); a.add_argument("key"); a.add_argument("--summary"); a.add_argument("--repos"); a.add_argument("--target", action="append"); a.set_defaults(fn=cmd_epic_set)
    a = ep.add_parser("note"); a.add_argument("key"); a.add_argument("text"); a.set_defaults(fn=cmd_epic_note)
    a = ep.add_parser("memory"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_memory)
    a = ep.add_parser("sync"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_sync)
    a = ep.add_parser("open"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_open)
    a = ep.add_parser("archive"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_archive)
    a = ep.add_parser("unarchive"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_unarchive)
    a = ep.add_parser("rm"); a.add_argument("key"); a.add_argument("--force", action="store_true"); a.set_defaults(fn=cmd_epic_rm)

    tk = sub.add_parser("task").add_subparsers(dest="cmd", required=True)
    a = tk.add_parser("add"); a.add_argument("epic"); a.add_argument("title")
    a.add_argument("--prompt", required=True); a.add_argument("--repos")
    a.add_argument("--sync", action="store_true"); a.add_argument("--no-setup", action="store_true")
    a.add_argument("--jira", help="link to an EXISTING Jira issue key instead of creating one")
    a.add_argument("--no-jira", action="store_true", help="do not create a Jira task")
    a.set_defaults(fn=cmd_task_add)
    a = tk.add_parser("ls"); a.add_argument("epic", nargs="?"); a.set_defaults(fn=cmd_task_ls)
    a = tk.add_parser("diff"); a.add_argument("task"); a.set_defaults(fn=cmd_task_diff)
    a = tk.add_parser("open"); a.add_argument("task"); a.set_defaults(fn=cmd_task_open)
    a = tk.add_parser("done"); a.add_argument("task"); a.add_argument("--force", action="store_true"); a.set_defaults(fn=cmd_task_done)
    a = tk.add_parser("mr"); a.add_argument("task"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--no-ai", action="store_true", help="static commit/MR text (skip claude generation)"); a.set_defaults(fn=cmd_task_mr)
    a = tk.add_parser("mrs"); a.add_argument("task"); a.set_defaults(fn=cmd_task_mrs)
    a = tk.add_parser("abort"); a.add_argument("task"); a.set_defaults(fn=cmd_task_abort)
    return p

# commands that only read state -> no lock; everything else mutates -> serialize
_READONLY = {cmd_task_diff, cmd_task_ls, cmd_repo_ls, cmd_repo_members,
             cmd_epic_ls, cmd_epic_memory, cmd_project_ls, cmd_jira_epics}

_SELF_LOCKED = {cmd_epic_mrs, cmd_task_mrs}   # do network lock-free, then save under a brief lock themselves

def main(argv=None):
    args = build_parser().parse_args(argv)
    fn = args.fn
    if fn in _READONLY or fn in _SELF_LOCKED:
        fn(args)
    else:
        with state_lock():
            fn(args)

if __name__ == "__main__":
    main()
