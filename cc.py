#!/usr/bin/env python3
"""cc — multi-repo, epic-routed AI-agent orchestrator (Phase 1 engine).

Model:  Project > Repos   and   Project > Epic > Task > RepoWork
Rule:   the agent only EDITS files; cc owns ALL git (branch/commit/push/MR).
State:  ~/.cc/state.json holds intent + pointers; git/jsonl hold the truth.
"""
import argparse, base64, fcntl, json, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time, urllib.request
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

def _valid_state(d):
    return isinstance(d, dict) and all(isinstance(d.get(k), dict) for k in ("projects", "epics", "tasks"))

def load_state():
    if not STATE_FILE.exists():
        return {"projects": {}, "epics": {}, "tasks": {}}
    try:
        d = json.loads(STATE_FILE.read_text())
        if not _valid_state(d):
            raise ValueError("missing projects/epics/tasks")
        return d
    except Exception as e:
        # state.json is torn/corrupt. NEVER fall back to empty — that would let the next save overwrite
        # everything. Self-heal from the newest VALID backup (the snapshots that saved us before).
        bk = _newest_valid_backup()
        if bk:
            data = json.loads(bk.read_text())
            try:
                shutil.copy2(str(bk), STATE_FILE)   # restore the good snapshot in place
            except Exception:
                pass
            audit("state.restore", detail=bk.name, reason="corrupt-state")
            sys.stderr.write("⚠️ ~/.cc/state.json был повреждён (%s) — восстановлен из бэкапа %s\n"
                             % (str(e)[:60], bk.name))
            return data
        die("~/.cc/state.json повреждён и нет валидного бэкапа (%s). Проверь ~/.cc/backups/." % str(e)[:80])

_BACKUP_DIR = STATE_DIR / "backups"

def _backup_prev(prev):
    """Snapshot the PREVIOUS on-disk state before we overwrite it, but only when this write changes
    the SET of epics/tasks (an add or — the dangerous case — a removal). Status-only churn is
    ignored so we don't spam. Keeps the newest 80 snapshots; recovery = copy one back."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = _BACKUP_DIR / ("state-%s-%d.json" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        if not dst.exists():
            dst.write_text(json.dumps(prev, indent=2, ensure_ascii=False))
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
        for old in sorted(_BACKUP_DIR.glob("state-*.json"))[:-80]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass

def _newest_valid_backup():
    """Newest ~/.cc/backups/ snapshot that parses as a valid state — for self-heal of a corrupt state."""
    try:
        for bk in sorted(_BACKUP_DIR.glob("state-*.json"), reverse=True):
            try:
                if _valid_state(json.loads(bk.read_text())):
                    return bk
            except Exception:
                continue
    except Exception:
        pass
    return None

# ----------------------------- audit timeline -----------------------------
AUDIT_FILE = STATE_DIR / "audit.log"
_AUDIT_MAX = 4_000_000          # ~4MB; past that, trim to the newest _AUDIT_KEEP lines
_AUDIT_KEEP = 3000

def audit(action, **fields):
    """Append one record to ~/.cc/audit.log — the 'what happened' timeline (JSONL, append-only, so it
    can't tear like state.json could). BEST-EFFORT: never raises, so a logging hiccup can't break a
    real git/jira action. Never pass secrets (no tokens) — these are plain, readable history lines."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "action": action}
        for k, v in fields.items():
            if v not in (None, [], {}, ""):
                rec[k] = v
        new = not AUDIT_FILE.exists()
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if new:
            try:
                os.chmod(AUDIT_FILE, 0o600)
            except OSError:
                pass
        if AUDIT_FILE.stat().st_size > _AUDIT_MAX:
            _trim_audit()
    except Exception:
        pass

def _trim_audit():
    """Keep the log bounded: rewrite (atomically) with only the newest _AUDIT_KEEP lines."""
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-_AUDIT_KEEP:]
        tmp = AUDIT_FILE.with_name(AUDIT_FILE.name + ".tmp.%d" % os.getpid())
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, AUDIT_FILE)
    except Exception:
        pass

def read_audit(task=None, epic=None, since=None, action=None, limit=300):
    """Timeline records, NEWEST first, optionally filtered by task/epic/action/since(epoch seconds)."""
    recs = []
    try:
        for ln in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if task and r.get("task") != task:
                continue
            if epic and r.get("epic") != epic:
                continue
            if action and r.get("action") != action:
                continue
            if since and r.get("ts", 0) < since:
                continue
            recs.append(r)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    recs.reverse()
    return recs[:limit]

def save_state(s):
    """Atomic write (tmp + os.replace) so concurrent readers never see a torn file.
    Also snapshots the previous state to ~/.cc/backups/ whenever the epic/task SET changes, so a
    dropped epic or task is always recoverable (see `cc recover` / `cc orphans`)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if STATE_FILE.exists():
            prev = json.loads(STATE_FILE.read_text())
            if (set(prev.get("tasks", {})) != set(s.get("tasks", {}))
                    or set(prev.get("epics", {})) != set(s.get("epics", {}))):
                _backup_prev(prev)
    except Exception:
        pass
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    try:
        os.chmod(tmp, 0o600)          # may hold a Jira API token
    except OSError:
        pass
    os.replace(tmp, STATE_FILE)       # atomic on POSIX

_LOCK_PATH = STATE_DIR / ".state.lock"
_lock_tl = threading.local()   # per-thread recursion depth: fcntl.flock is NOT reentrant across
                               # fds, so a nested state_lock() in the same thread (e.g. main() wraps
                               # a command that then calls mutate()) would deadlock on itself.

@contextmanager
def state_lock():
    """Exclusive cross-process lock so load->modify->save can't lose updates. REENTRANT within a
    thread (nested acquisition reuses the held lock instead of deadlocking). Held only around
    mutations; reads stay lock-free (writes are atomic)."""
    depth = getattr(_lock_tl, "depth", 0)
    if depth > 0:                       # this thread already holds it -> reuse, don't re-flock
        _lock_tl.depth = depth + 1
        try:
            yield
        finally:
            _lock_tl.depth -= 1
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        _lock_tl.depth = 1
        yield
    finally:
        _lock_tl.depth = 0
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

def mutate_try(fn):
    """Non-blocking mutate for BACKGROUND pollers: if the lock is busy, skip (return False) instead
    of queuing — so the TUI's periodic writes never starve a foreground `cc` command (fcntl flock
    isn't fair). Best-effort: the next tick retries. Returns True if it wrote."""
    if getattr(_lock_tl, "depth", 0) > 0:   # this thread already holds the lock -> just mutate
        s = load_state(); fn(s); save_state(s); return True
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(_LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        s = load_state()
        fn(s)
        save_state(s)
        return True
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()

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

def _ahead_count(rp, branch, base):
    """How many commits `branch` has that `base` (master/main) doesn't — i.e. real changes to bring
    in. 0 means the epic branch is identical to master (no tasks merged into it yet) -> no MR."""
    bref = branch if have_ref(rp, branch) else ("origin/" + branch if have_ref(rp, "origin/" + branch) else None)
    if not bref:
        return 0
    baseref = ("origin/" + base) if have_ref(rp, "origin/" + base) else base
    out = run(["git", "rev-list", "--count", baseref + ".." + bref], cwd=rp, check=False).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return 0

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
    # the repo's TRUE default branch (origin/HEAD), NOT whatever branch is checked out now — capturing
    # the current branch made tasks base off random feature branches (e.g. invictusgo on
    # `feat-add-website-source`) and produce MRs carrying the whole unrelated history.
    db = default_branch(p)
    setup, run_cmd = detect_setup_run(p)
    return {"path": str(p), "provider": provider, "remote": remote, "default_branch": db,
            "setup": setup, "run": run_cmd, "reviewer": ""}

def detect_setup_run(repo_path):
    """Best-effort defaults so a fresh worktree is runnable.
    node repos: give the worktree its OWN node_modules via a copy-on-write CLONE of the main
    checkout's (APFS clonefile on macOS, reflink on btrfs/xfs) — instant like a symlink but
    ISOLATED. A symlink made every worktree + the main repo share one node_modules, which breaks
    Vite/esbuild (shared `.vite` cache, binary resolution through the link) and makes `npm install`
    in a worktree corrupt the shared one. Clone fixes all that; falls back to symlink if CoW fails."""
    pj = Path(repo_path) / "package.json"
    if pj.exists():
        run_cmd = ""
        try:
            scripts = json.loads(pj.read_text()).get("scripts", {})
            run_cmd = "npm run dev" if "dev" in scripts else ("npm start" if "start" in scripts else "")
        except Exception:
            pass
        setup = ('if [ -d "$CC_MAIN_REPO/node_modules" ]; then rm -rf node_modules; '
                 'cp -Rc "$CC_MAIN_REPO/node_modules" node_modules 2>/dev/null '          # macOS APFS clonefile
                 '|| cp -a --reflink=auto "$CC_MAIN_REPO/node_modules" node_modules 2>/dev/null '  # Linux CoW
                 '|| ln -sfn "$CC_MAIN_REPO/node_modules" node_modules; fi')               # fallback: symlink
        return setup, run_cmd
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
        return "empty", {}, path     # empty folder -> a new project you fill via `cc repo add`
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
    """Find the newest claude session transcript for a directory (lazy, on open). Claude encodes the
    cwd into the projects-dir name by replacing every non [A-Za-z0-9-] char with '-' (so `/`, `.`,
    `_` all become '-', e.g. `.cc-setup` -> `-cc-setup`, `_release` -> `-release`). A naive
    replace('/','-') missed dirs with dots/underscores (project & epic chats) -> always-new sessions."""
    enc = re.sub(r"[^A-Za-z0-9-]", "-", str(primary_wt))
    d = CLAUDE_PROJECTS / enc
    if not d.exists():
        return None
    js = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return js[0].stem if js else None

# ----------------------------- project cmds -----------------------------

SETUP_RUNBOOK = """# Project {project} — setup & build conductor

This chat is the COMMAND CENTER for project {project} (folder: {base}). You both (1) bootstrap
the project and (2) drive ongoing development from here: build the MVP directly, and when work
splits into chunks or point features/bugs arrive, generate epics + tasks and run them. The user
watches everything in `cc tui`; you orchestrate from here.

cc owns project structure & git. Drive cc via the `cc` CLI (if `cc` is not on PATH, use:
  python3 {ccpy} <args>). Interview & confirm before big moves — don't assume purpose or stack.
Existing repos: {repos}

## Phase 1 — bootstrap (first time)
1. Interview: what is this project & for whom? core features? tech stack (yours or recommend)?
   which repos and their roles (kept as SEPARATE repos)? any code references/conventions? Summarize, get a yes.
2. Create repos:  cc repo add {project} <repo-name> --new --run "<dev command>"
   (remote stays empty; set later: cc repo set {project} <repo-name> --remote <group/proj>.
    existing local repo: --path <dir>; clone: --clone <url>.)
3. Scaffold each repo's starter code per the agreed stack (framework init, structure, lint/test,
   .gitignore, a minimal runnable skeleton) following the references.
4. Make it AI-ready — VERSION context INSIDE the repos, NOT the container folder. The folder
   holding the repos is NOT a git repo, so anything there (CLAUDE.md, .claude/skills) is local-only,
   unversioned, invisible to teammates. So:
   - `<repo>/CLAUDE.md` (committed) = SOURCE OF TRUTH per repo: stack, conventions, structure.
   - Cross-repo project context (what it is, how repos relate, shared invariants) → a concise
     "## Project context" block at the TOP of EACH repo's CLAUDE.md (self-contained when cloned alone).
   - Project skills → `<repo>/.claude/skills/` (committed with the repo), not the container.
   - `{base}/CLAUDE.md` in the container is OPTIONAL, LOCAL-ONLY (a memo for this chat) — never the
     source of truth. MCP/settings: per repo or user-scope, not the container.
5. Set commands & commit:  cc repo set {project} <repo> --run "..." --setup "..."  then commit the scaffold.

## Phase 2 — build & orchestrate (the ongoing work)
Structure REAL work (the MVP, a feature, a bug) as an EPIC on the board with TASKS — don't
silently build into main. After you and the user agree WHAT to build, you **MUST ASK which of
two execution modes** they want, and WAIT for their answer before doing anything:

  First decide GROUPING by the project: if it uses epics, group tasks under an epic; if it works
  PURELY on tasks (no epic — e.g. a single-app project), create epic-LESS tasks with the PROJECT as
  the first arg. The two modes below are orthogonal to grouping (each shows both forms).

  ▸ MODE 1 — parallel BACKGROUND agents:
    Each task → its OWN headless `claude -p` in an isolated worktree; they run in parallel while you
    keep going; the user reviews/merges each in cc tui. Each `task add` LAUNCHES immediately.
      epic project:  cc epic add {project} <KEY> --summary "<area>"   (or reuse an epic)
                     cc task add <KEY> "<title>" --prompt "<what>" --no-jira [--repos r1,r2]
      epic-less:     cc task add {project} "<title>" --prompt "<what>" --no-jira [--repos r1,r2]

  ▸ MODE 2 — I build each task MYSELF on its branch for review (no auto-merge):
    I do the work in THIS session on each TASK's own branch — never on a repo's default branch — so
    the user can test / fix BEFORE merge. Tasks show on the board. Add --manual (board task + worktree,
    NO bg agent — I do it; the worktree dir is printed; run/test/commit there):
      epic project:  cc epic add {project} <KEY> --summary "<area>"
                     cc task add <KEY> "<title>" --prompt "<plan>" --no-jira --manual
      epic-less:     cc task add {project} "<title>" --prompt "<plan>" --no-jira --manual
    Each task's MR targets its branch's base: the epic integration branch, or for epic-less the
    `cc project target` collect branch (else the repo default). The user merges in cc tui.

ALWAYS present both and ask: "1 — фоновые сессии, или 2 — я строю на ветке для ревью?" Do not
proceed until the user picks. (A truly trivial one-liner can be done directly — use judgement.)

## Epic-less projects (work purely on tasks)
If this project doesn't use epics, create tasks with the PROJECT as the first arg — no epic:
  cc task add {project} "<title>" --prompt "<...>" [--manual] [--no-jira]
These attach to the project and MR to each repo's default branch. To COLLECT them into an
integration branch instead (e.g. one open "everything" MR's branch), set it once:
  cc project target {project} <repo>=<branch>     # e.g. web=feature/api-integrations
  cc project target {project}                     # show current   ·   --clear to revert to default
New epic-less tasks then MR into that branch (existing tasks keep their original target).

Status / visibility (both modes):
  cc epic ls {project}        # epics here       cc task diff <task-id>   # a task's diff
  cc task mrs <task-id>       # per-task state
The user sees every epic/task live in cc tui and reviews/merges there.

## Releasing from THIS project chat
Releasing straight from here (merge -> main + deploy) is the LESS-SAFE path: there's no per-repo MR
to review and no isolated branch. Use it only for (a) work you did in THIS session, or (b) greenfield
with no remote yet (then "release" = a local merge to main, no prod). For team prod releases prefer
the layered flow: task chats land work, then release per EPIC (`cc epic mr` -> merge -> deploy,
respecting the release train) — never merge to main without the user's explicit OK.
Whichever path: ACTUALIZE first (merge the latest `origin/<default>` into the branch so you ship on
top of current main, not a stale base), and if the repo is versioned bump to a genuinely NEW version
(never reuse an existing version/tag).

## Constraints
- Propose, confirm, THEN build. Never pick a stack, restructure, or commit to main silently.
- Real work = epic + tasks on the board; ALWAYS ask mode 1 vs 2 first.
- No remote yet -> MRs are skipped; integration = a local merge on the USER's decision.
- Keep it simple and fast — smallest thing that works over heavy process.
"""

def cmd_project_setup(args):
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    base = Path(proj["path"]); base.mkdir(parents=True, exist_ok=True)
    sdir = base / ".cc-setup"; sdir.mkdir(parents=True, exist_ok=True)
    repos = list(proj.get("repos", {}).keys())
    runbook = SETUP_RUNBOOK.format(
        project=args.project, base=str(base), ccpy=os.path.abspath(__file__),
        repos=(", ".join(repos) if repos else "(пока нет — создашь в этом чате)"))
    runbook += jira_chat_setup(sdir, proj, args.project)   # project-scoped Jira + block the MCP
    (sdir / "CLAUDE.md").write_text(runbook)
    print("setup chat dir: %s" % sdir)
    print("  open:  cd %s && claude --permission-mode auto --add-dir %s%s"
          % (shlex.quote(str(sdir)), shlex.quote(str(base)), chat_jira_flags(proj, args.project)))

def cmd_project_target(args):
    """Where epic-LESS (loose) tasks of this project send their MRs. Default = each repo's default
    branch; set repo=branch to collect work into an integration branch instead (e.g. point web at the
    branch behind an open 'collect everything' MR). Affects NEW loose tasks; existing ones keep the
    target baked at creation. Stored on the project's hidden loose container."""
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    key = ensure_loose_epic(s, args.project)
    e = s["epics"][key]
    if args.clear:
        e["targets"] = {}
        save_state(s)
        print("loose-task MR target очищен — задачи без эпика снова льют в дефолтную ветку репо")
    elif args.spec:
        targets = dict(e.get("targets") or {})
        for pair in args.spec:
            if "=" not in pair:
                die("ожидается repo=branch, получено '%s'" % pair)
            r, br = pair.split("=", 1)
            if r not in proj["repos"]:
                die("repo '%s' нет в проекте '%s' (repos: %s)" % (r, args.project, ", ".join(proj["repos"])))
            targets[r] = br
        e["targets"] = targets
        save_state(s)
        print("loose-task MR target обновлён")
    cur = e.get("targets") or {}
    print("MR-таргет задач БЕЗ эпика для '%s':" % args.project)
    for r, ri in proj["repos"].items():
        tgt = cur.get(r)
        print("  %-12s -> %s" % (r, tgt if tgt else (ri.get("default_branch", "?") + "  (default)")))
    print("(новые задачи без эпика льют MR сюда; существующие сохраняют свой таргет на момент создания)")

def cmd_project_new(args):
    """Create a brand-new EMPTY project (the folder too), ready to fill with repos via `cc repo add`."""
    base = (Path(args.path).expanduser().resolve() if args.path
            else (Path.home() / "Codebase" / "work" / args.name))
    base.mkdir(parents=True, exist_ok=True)
    def fn(s):
        if args.name in s["projects"]:
            die("project '%s' already exists" % args.name)
        s["projects"][args.name] = {"path": str(base), "kind": "multi", "repos": {},
                                    "default_assignee": glab_user()}
    mutate(fn)
    print("пустой проект '%s' создан (%s).\n"
          "Добавь репо раздельно: `cc repo add %s frontend --new`, `cc repo add %s backend --new`.\n"
          "Remote проставишь позже: `cc repo set %s <repo> --remote <group/proj>`."
          % (args.name, base, args.name, args.name, args.name))

def cmd_repo_add(args):
    """Add a repo to an existing project: --new (git init fresh), --clone <url>, or --path <dir>."""
    s = load_state()
    proj = s["projects"].get(args.project) or die("unknown project '%s'" % args.project)
    base = Path(proj["path"])
    name = args.name
    if name in proj["repos"]:
        die("repo '%s' уже в проекте '%s'" % (name, args.project))
    if args.path:
        dest = Path(args.path).expanduser().resolve()
        if not (dest / ".git").exists():
            die("не git-репозиторий: %s" % dest)
    elif args.clone:
        dest = base / name
        base.mkdir(parents=True, exist_ok=True)
        print("clone %s -> %s …" % (args.clone, dest))
        run(["git", "clone", args.clone, str(dest)], check=True, timeout=600)
    else:  # --new (default): fresh git init
        dest = base / name
        if dest.exists() and any(dest.iterdir()):
            die("%s уже существует и не пуст" % dest)
        dest.mkdir(parents=True, exist_ok=True)
        run(["git", "init", str(dest)], check=True)
        c = run(["git", "commit", "--allow-empty", "-m", "chore: init %s" % name], cwd=dest, check=False)
        if c.returncode != 0:
            print("  (заметка: пустой initial commit не создан — настрой git user.name/email; "
                  "без коммита worktrees задач не создадутся)")
    info = repo_info(dest)
    if args.remote:
        info["remote"] = args.remote
        info["provider"] = ("gitlab" if "gitlab" in args.remote
                            else "github" if "github" in args.remote else info.get("provider", "unknown"))
    if args.run:
        info["run"] = args.run
    def fn(st):
        st["projects"][args.project]["repos"][name] = info
    mutate(fn)
    tail = "" if info.get("remote") else ("  — remote пуст: `cc repo set %s %s --remote <group/proj>`"
                                          % (args.project, name))
    print("repo '%s' добавлен в '%s' (%s)%s" % (name, args.project, dest, tail))

def cmd_project_add(args):
    s = load_state()
    kind, repos, path = detect_repos(args.path)
    name = args.name or path.name
    s["projects"][name] = {"path": str(path), "kind": kind, "repos": repos,
                           "default_assignee": glab_user()}
    save_state(s)
    if kind == "empty":
        print("проект '%s' создан ПУСТЫМ (%s — репозиториев пока нет).\n"
              "Добавь репо раздельно: `cc repo add %s frontend --new`, `cc repo add %s backend --new` "
              "(или --clone <url> / --path <dir>). Remote позже: `cc repo set %s <repo> --remote <slug>`."
              % (name, path, name, name, name))
        return
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
    # epic keys are GLOBAL — refuse to silently overwrite an epic that belongs to another project
    # (re-adding under the SAME project is fine; that restores stashed knowledge).
    if args.key in s["epics"] and s["epics"][args.key].get("project") not in (None, args.project):
        die("epic '%s' уже существует под проектом '%s' — ключи эпиков глобальны; возьми другой ключ"
            % (args.key, s["epics"][args.key]["project"]))
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
    # restore knowledge if this epic was finished earlier (cc epic done stashed it)
    restored = (s.get("epic_knowledge") or {}).get(args.key)
    summary = args.summary or ""
    memory = summary.strip()
    if restored:
        memory = restored.get("memory") or memory
        if not summary:
            summary = restored.get("summary", "")
        if not targets and restored.get("targets"):
            targets = dict(restored["targets"]); mode = "targets"
    s["epics"][args.key] = {"project": args.project, "summary": summary,
                            "targets": targets, "mode": mode, "branch": args.key, "repos": erepos,
                            "memory": memory}
    n = sync_epic_children(s, args.key)
    save_state(s)
    print("epic '%s' added under '%s'  [mode=%s]" % (args.key, args.project, mode))
    if restored:
        nlines = len([l for l in (memory or "").splitlines() if l.strip()])
        print("  ↻ восстановлены знания закрытого эпика (закрыт %s): %d стр. заметок"
              % (restored.get("done_at", "?"), nlines))
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
        if e.get("loose"):
            continue   # hidden per-project container for epic-less tasks
        mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
        print("%s  [%s, %s]  %s" % (key, e["project"], mode, e.get("summary", "")))
        if mode == "epic_branch":
            print("    tasks -> branch '%s' -> MR to master/main" % key)
        for r, b in e.get("targets", {}).items():
            print("    %-22s -> %s" % (r, b))

# ----------------------------- task cmds -----------------------------

def target_for(epic, proj, repo):
    return epic.get("targets", {}).get(repo) or proj["repos"][repo]["default_branch"]

def mr_target_for(epic_key, epic, proj, repo):
    """The MR target branch a task in this group would use — mirrors _provision: epic_branch mode
    targets the epic's own branch; targets/loose mode targets the per-repo integration branch or the
    repo default. Used when a task is (re)grouped so its base reflects the new group."""
    mode = epic.get("mode") or ("targets" if epic.get("targets") else "epic_branch")
    if mode == "epic_branch":
        return epic_key
    return target_for(epic, proj, repo)

def worktree_path(project_path, epic, slug, repo_name):
    return Path(project_path) / "cctui" / epic / slug / repo_name

def _provision(epic_key, epic, proj, r, branch, slug, epic_mode, no_setup):
    ri = proj["repos"][r]; rp = ri["path"]
    # worktrees are LOCAL (epic branch + `git worktree add`); a remote is only needed at MR time
    # (which skips no-remote repos gracefully). So greenfield repos with no remote still work.
    if not rp or not os.path.isdir(rp) or not (Path(rp) / ".git").exists():
        return (r, None, None, "not a git repo")
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


def _unique_tid(s, epic, slug):
    """A task state-key that can NEVER overwrite an existing one. `t_<slug>` normally; if that key
    is taken by a task in a DIFFERENT epic, scope it by epic; then bump a numeric suffix until free.
    (Same-slug-different-epic used to silently clobber a task — the IK-8894-go loss.)"""
    tid = "t_" + slug
    if tid in s["tasks"] and s["tasks"][tid].get("epic") != epic:
        tid = "t_" + slugify(epic) + "_" + slug
    base, i = tid, 2
    while tid in s["tasks"]:
        tid = base + "-" + str(i); i += 1
    return tid

def loose_epic_key(project):
    return project + "__loose"

def ensure_loose_epic(s, project):
    """A hidden per-project container for epic-LESS tasks. It's an ordinary epic with empty targets
    (so target_for -> the repo default branch, i.e. MRs go straight to master/main) flagged loose=True.
    Hidden from the tree (its tasks render directly under the project) and from `cc epic ls`. Lets a
    project run on plain tasks with no Jira epic — the work attaches to the project and to main."""
    key = loose_epic_key(project)
    if key not in s["epics"]:
        s["epics"][key] = {"project": project, "summary": "", "mode": "targets",
                           "targets": {}, "loose": True}
    return key

def _unique_branch(s, slug):
    """Plain `<slug>` branch for a loose task; suffix -2/-3… if another task already uses it."""
    used = {t.get("branch") for t in s["tasks"].values()}
    if slug not in used:
        return slug
    i = 2
    while ("%s-%d" % (slug, i)) in used:
        i += 1
    return "%s-%d" % (slug, i)

def render_task_claude_md(s, tid):
    """The task chat's CLAUDE.md (rules + repo map + release model), rebuilt from CURRENT state and
    templates. PURE — returns the markdown. Used at task creation AND on every chat reopen so the rules
    are NEVER stale (this is the answer to 'do I reopen chats after changing rules?' — no, cc refreshes
    them on open, same as `cc epic open` / `cc project setup` already do)."""
    t = s["tasks"][tid]
    ekey = t["epic"]; epic = s["epics"][ekey]; loose = bool(epic.get("loose"))
    proj = s["projects"][epic["project"]]
    order = t.get("repos", [])
    worktrees = t.get("worktrees", {})
    branch = t.get("branch", "")
    task_dir = t.get("dir") or (str(Path(next(iter(worktrees.values()))).parent) if worktrees else "")
    repo_map = "\n".join("- %s -> %s" % (r, worktrees.get(r, "?")) for r in order)
    epic_mem = (epic.get("memory") or "").strip()
    header = ("# Task in project %s (no epic) -> MR to master/main" % epic["project"]) if loose \
             else ("# Epic %s: %s" % (ekey, epic.get("summary", "")))
    md = ("%s\n\n%s\n\n## Repos available for THIS task (branch %s):\n%s\n\n"
          "Touch ONLY the repos actually relevant to this task; leave the rest UNCHANGED "
          "(cc opens a Merge Request only for repos you modify, so untouched repos cost nothing). "
          "Do NOT run git — cc handles branches/commits/MRs.\n") % (
          header, epic_mem or "(no epic notes yet)", branch, repo_map)
    if loose:
        md += (
            "\n## Release (when the user says релиз / merge / залей)\n"
            "This task has NO epic — its MRs target master/main, so merging it IS a prod-bound release.\n"
            "- `cc task mrs %s` — show each repo's MR (the user reviews it).\n"
            "- Before merging, ACTUALIZE: `git fetch origin <default>` + merge `origin/<default>` into the\n"
            "  task branch (resolve conflicts) so you ship ON TOP of current master/main, not a stale base.\n"
            "- `cc task merge %s` — merge the OPEN MRs into master/main (`--dry-run` to preview).\n"
            "- If the repo is versioned, bump to a genuinely NEW version (current version + latest `git tag`,\n"
            "  increment, never reuse; verify the tag is free) and tag it.\n"
            "Merge ONLY on the user's explicit word — never merge to a default branch on your own. After the\n"
            "merge it is PROD-bound: deploy per the project's release process (release train — backend+mobile\n"
            "land on prod before web/admin; feature-flag-gated surfaces ship dark). Local-only repos with no\n"
            "remote just merge locally (no prod).\n") % (tid, tid)
    else:
        md += (
            "\n## Release (when the user says релиз / merge / залей)\n"
            "This is an EPIC task — its MRs target the epic's branch (%s), NOT master/main. Merging it only\n"
            "LANDS the work INTO the epic; it is NOT a prod release.\n"
            "- `cc task mrs %s` — show the MR (the user reviews it).\n"
            "- `cc task merge %s` — merge it into the epic branch, on the user's word.\n"
            "The PROD release of the WHOLE epic happens in the EPIC chat (`cc epic mr` → merge → deploy,\n"
            "respecting the release train) — not from this task chat.\n") % (ekey, tid, tid)
    md += jira_chat_setup(task_dir, proj, epic["project"])   # project-scoped Jira + block the MCP
    return md

def write_task_claude_md(s, tid):
    """(Re)write the task's CLAUDE.md from render_task_claude_md. Best-effort -> bool."""
    t = s["tasks"].get(tid)
    if not t:
        return False
    wts = t.get("worktrees") or {}
    task_dir = t.get("dir") or (str(Path(next(iter(wts.values()))).parent) if wts else None)
    if not task_dir:
        return False
    try:
        (Path(task_dir) / "CLAUDE.md").write_text(render_task_claude_md(s, tid))
        return True
    except Exception:
        return False

def cmd_task_setup(args):
    """Regenerate the task's CLAUDE.md so its rules are FRESH (called on chat reopen by the TUI).
    Mirrors `cc epic open` / `cc project setup`, which already rebuild their runbooks on open."""
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    if write_task_claude_md(s, args.task):
        print("task %s — CLAUDE.md обновлён (правила свежие): %s/CLAUDE.md" % (args.task, t.get("dir", "?")))
    else:
        die("не смог записать CLAUDE.md (нет dir/worktree у задачи?)")

def cmd_task_regroup(args):
    """Move a task to another GROUP (epic) within the SAME project, recomputing its MR targets. The
    branch and worktrees are untouched — only group membership + per-repo base change (single-membership:
    a task is in exactly one group). Pass a project name to UNGROUP (move to the project's loose group).
    Refuses cross-project moves and tasks that already have an MR (an open MR's target won't change)."""
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj_name = s["epics"][t["epic"]]["project"]
    tgt = args.group
    if tgt in s["epics"]:
        new_key = tgt
        if s["epics"][new_key].get("project") != proj_name:
            die("группа '%s' в проекте '%s' — перенос только внутри проекта '%s'" % (
                new_key, s["epics"][new_key].get("project"), proj_name))
    elif tgt in s["projects"]:
        if tgt != proj_name:
            die("проект '%s' не тот, в котором задача (проект '%s')" % (tgt, proj_name))
        new_key = ensure_loose_epic(s, proj_name)          # ungroup -> the project's loose container
    else:
        die("неизвестная группа/эпик/проект '%s'" % tgt)
    if new_key == t["epic"]:
        die("задача уже в группе '%s'" % new_key)
    if t.get("mrs"):
        die("у задачи уже есть MR — перенос только для задач без MR (target открытого MR не сменится автоматически).\n"
            "Закрой/смёржи MR (cc task abort/merge), потом переноси.")
    proj = s["projects"][proj_name]
    new_epic = s["epics"][new_key]
    old_key = t["epic"]
    t["epic"] = new_key
    t["base"] = {r: mr_target_for(new_key, new_epic, proj, r) for r in t["repos"]}
    save_state(s)
    write_task_claude_md(s, args.task)                     # rules reflect the new group's release model
    audit("task.regroup", task=args.task, **{"from": old_key, "to": new_key})
    loose = bool(new_epic.get("loose"))
    print("task %s перенесена: %s -> %s%s" % (args.task, old_key, new_key, "  (без группы)" if loose else ""))
    for r in t["repos"]:
        print("  %s -> %s" % (r, t["base"][r]))

def cmd_task_add(args):
    s = load_state()
    # `args.epic` may be a real epic key OR a PROJECT name — the latter creates an epic-LESS task that
    # attaches directly to the project and targets master/main (via a hidden loose container).
    ekey = args.epic
    if ekey not in s["epics"]:
        if ekey in s["projects"]:
            ekey = ensure_loose_epic(s, ekey)
        else:
            die("unknown epic or project '%s'" % args.epic)
    epic = s["epics"][ekey]
    loose = bool(epic.get("loose"))
    proj = s["projects"][epic["project"]]
    # title is optional — if you don't give one, claude writes a short one from the prompt
    title = (args.title or "").strip()
    if not title:
        if not (args.prompt or "").strip():
            die("нужен title ИЛИ --prompt (из промпта cc сам придумает название)")
        print("  название не задано — генерирую из промпта …")
        title = gen_task_title(args.prompt, cwd=proj.get("path"))
        print("  название: %s" % title)
    repos = args.repos.split(",") if args.repos else (epic.get("repos") or list(proj["repos"].keys()))
    for r in repos:
        if r not in proj["repos"]:
            die("repo '%s' not in project '%s'" % (r, epic["project"]))
    slug = slugify(title)
    branch = _unique_branch(s, slug) if loose else ("%s-%s" % (ekey, slug))
    tid = _unique_tid(s, ekey, slug)
    epic_mode = epic.get("mode") or ("targets" if epic.get("targets") else "epic_branch")
    where = ("project %s (no epic) -> master/main" % epic["project"]) if loose else ("epic %s [%s]" % (ekey, epic_mode))
    print("task '%s' under %s - provisioning %d repo(s) in parallel ..." % (title, where, len(repos)))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(repos)))) as exr:
        results = list(exr.map(
            lambda r: _provision(ekey, epic, proj, r, branch, slug, epic_mode, args.no_setup), repos))
    worktrees, base, skipped, order = {}, {}, {}, []
    for (r, wt, tgt, err) in results:
        if err:
            print("  [%s] skipped (%s)" % (r, err)); skipped[r] = err; continue
        worktrees[r] = wt; base[r] = tgt; order.append(r)
        print("  [%s] ready -> MR target %s" % (r, tgt))
    if not worktrees:
        hint = ("cc repo set %s <repo> --remote …" % epic["project"]) if loose else ("cc epic set %s --repos <a,b>" % ekey)
        die("no usable repos (all skipped). %s" % hint)
    if skipped:
        # keep the skipped repos VISIBLE (stored on the task, shown in the TUI) — never a silent gap
        print("  ⚠️ %d repo(s) NOT provisioned: %s" % (len(skipped), ", ".join("%s (%s)" % (r, e) for r, e in skipped.items())))
    primary = order[0]
    task_dir = str(Path(worktrees[primary]).parent)   # cctui/<epic>/<slug>/ — repos are its subfolders
    repo_map = "\n".join("- %s -> %s" % (r, worktrees[r]) for r in order)   # used by the agent's full_prompt below
    log = STATE_DIR / ("%s.log" % tid)
    s["tasks"][tid] = {"epic": ekey, "title": title, "prompt": args.prompt,
                       "repos": order, "branch": branch, "worktrees": worktrees, "base": base,
                       "primary": primary, "dir": task_dir, "claude_session": {}, "status": "running",
                       "mrs": {}, "log": str(log), "skipped": skipped}
    save_state(s)
    write_task_claude_md(s, tid)   # rules from current templates — SAME path as a chat reopen
    audit("task.add", task=tid, epic=ekey, repos=order, branch=branch, title=title, skipped=skipped)
    full_prompt = (args.prompt
                   + "\n\n[cc] This task spans the repo worktrees below (branch %s). "
                     "Edit files ONLY inside these subfolders (your cwd is their parent):\n%s"
                     "\nDo NOT run git; cc handles branches/commits/MRs."
                     "\n\n[cc] You run NON-INTERACTIVELY in the background — there is NO ONE to answer "
                     "questions right now. NEVER call AskUserQuestion and never wait for input. If "
                     "something is ambiguous, pick the most reasonable option, STATE the assumption in "
                     "one line, and KEEP GOING until the task is fully done. Only if you genuinely "
                     "cannot finish without the user's decision: do everything you safely can first, "
                     "then make the VERY LAST line of your output exactly `[cc-needs-input] <one-line "
                     "question + your recommended default>` (nothing after it). The user sees that on "
                     "the board (❓) and answers when they open the chat." % (branch, repo_map))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if getattr(args, "manual", False):
        # MODE 2: no background agent — the command-center chat (or the user) does the work itself
        # in the worktree. Board-visible, isolated on its branch, never on main.
        s = load_state(); s["tasks"][tid]["status"] = "review"; s["tasks"][tid]["manual"] = True; save_state(s)
        print("  manual task (no bg agent) — do the work yourself in: %s" % task_dir)
    elif args.sync:
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
        if jira and jira.get("token") and not loose and ekey.split("-")[0] == jira.get("project_key"):
            try:
                cur = jira_issue_parent(jira, args.jira)
                if cur != ekey:
                    jira_set_parent(jira, args.jira, ekey)
                    print("  jira: %s -> moved under epic %s (was %s)" % (
                        args.jira, ekey, cur or "no parent"))
            except Exception as ex:
                print("  (jira reparent failed: %s)" % str(ex)[:80])
    elif jira and jira.get("token") and not loose and not getattr(args, "no_jira", False):
        try:
            jk = jira_create_task(jira, ekey, title, args.prompt)
            if jk:
                s = load_state(); s["tasks"][tid]["jira"] = jk; save_state(s)
                print("  jira task created: %s (under %s)" % (jk, ekey))
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

def _head_sha(wt):
    if not os.path.isdir(wt):
        return None
    r = run(["git", "rev-parse", "HEAD"], cwd=wt, check=False)
    return r.stdout.strip() if r.returncode == 0 else None

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def task_needs_input(t):
    """The background agent's question, if it finished by emitting `[cc-needs-input] <q>` as its last
    line (it can't pause to ask in headless -p). Reads only the log TAIL. Returns the text or None."""
    log = t.get("log")
    if not (log and os.path.exists(log)):
        return None
    try:
        with open(log, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    hits = re.findall(r"\[cc-needs-input\]\s*(.*)", tail)
    return (hits[-1].strip() or "агент ждёт твоего ответа") if hits else None

def task_status(t):
    """Derive status from reality:
       running (agent alive) > review (NEW local work) > merged > mr > review (committed, no MR) > idle.
    'NEW local work' = uncommitted changes anywhere, OR — for an already-merged task — commits beyond
    the point we recorded at merge time (merged_sha; squash-merge proof). So a task you already merged
    flips back to 🟡 the moment you touch it again, instead of staying ✅ forever."""
    pid = t.get("pid")
    if pid and pid_alive(pid):
        return "running"
    wts = t.get("worktrees", {})
    # uncommitted changes -> needs you, regardless of MR/merge state
    for r, wt in wts.items():
        if os.path.isdir(wt) and _changed(wt):
            return "review"
    if t.get("merged"):
        msha = t.get("merged_sha", {}) or {}
        for r, wt in wts.items():
            rec = msha.get(r)
            if rec and _head_sha(wt) not in (None, rec):   # new commits after the merge
                return "review"
        return "merged"
    if t.get("mrs"):
        return "mr"
    for r, wt in wts.items():
        if not os.path.isdir(wt):
            continue
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
        print("  follow-up:  (cd '%s' && claude --resume %s --permission-mode auto%s)" % (
            t["worktrees"][t["primary"]], sid, chat_jira_flags(proj, proj_name)))
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
    merged_sha = {}
    if all_merged:
        for r in t["repos"]:
            wt = t.get("worktrees", {}).get(r)
            sha = _head_sha(wt) if wt else None
            if sha:
                merged_sha[r] = sha
    def apply(st):
        tt = st["tasks"].get(args.task)
        if tt is not None:
            was = bool(tt.get("merged"))
            tt.setdefault("mrs", {}).update(found)
            tt["mr_state"] = states
            tt["merged"] = all_merged
            if all_merged:
                tt["merged_sha"] = merged_sha
                if not was or not tt.get("merged_at"):
                    tt["merged_at"] = time.time()   # for "recently closed first" ordering
            else:
                tt.pop("merged_at", None)
    mutate(apply)
    if not any_url:
        print("(no MRs found — create with M)")
    elif merged and not open_:
        print("\nall %d MR(s) merged -> `cc task done %s` to clean up worktrees" % (merged, args.task))
    elif merged:
        print("\n%d merged, %d still open" % (merged, open_))


def _task_merge_pass(t, proj, dry, squash, tid=None):
    """Merge the OPEN MR of each repo of task `t` (skip repos with no open MR). Prints per repo,
    returns {merged, failed, skipped}. Repos without changes simply have no open MR -> skipped."""
    branch = t["branch"]
    merged = failed = skipped = 0
    for r in t["repos"]:
        ri = proj["repos"].get(r)
        if not ri or not ri.get("remote"):
            print("  [%s] remote не задан — skip" % r); skipped += 1; continue
        mr = open_mr(ri["remote"], branch, ri["path"])
        if not mr:
            print("  [%s] нет открытого MR (ветка %s) — skip" % (r, branch)); skipped += 1; continue
        iid, tgt, url = mr.get("iid"), mr.get("target_branch"), mr.get("web_url")
        if dry:
            print("  [%s] DRY: merge !%s  (%s → %s)  %s" % (r, iid, branch, tgt, url)); continue
        ok, msg = merge_mr(ri["remote"], iid, ri["path"], squash=squash)
        if ok:
            print("  [%s] merged !%s  (%s → %s)" % (r, iid, branch, tgt)); merged += 1
            audit("task.merge", task=tid, epic=t.get("epic"), repo=r, mr=iid, base=tgt, branch=branch)
        else:
            print("  [%s] FAILED !%s: %s" % (r, iid, msg)); failed += 1
    return {"merged": merged, "failed": failed, "skipped": skipped}

def _refresh_task(tid):
    """Re-query & persist this task's MR state after a merge (reuses cmd_task_mrs)."""
    class _NS: pass
    ns = _NS(); ns.task = tid
    try:
        cmd_task_mrs(ns)
    except SystemExit:
        pass

def cmd_task_merge(args):
    # self-locked: glab merges are slow network; don't hold the global lock.
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj = s["projects"][s["epics"][t["epic"]]["project"]]
    print("merge task %s (branch %s)%s" % (args.task, t["branch"], "  [DRY-RUN]" if args.dry_run else ""))
    res = _task_merge_pass(t, proj, args.dry_run, args.squash, tid=args.task)
    print("  -> %d merged, %d failed, %d skipped" % (res["merged"], res["failed"], res["skipped"]))
    if not args.dry_run and res["merged"]:
        print("  refreshing MR state …")
        _refresh_task(args.task)


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
    """Готово и убрать с доски: Jira(task)->Done, snap local worktrees, drop the task from cc.
    Remote MR/branch are left intact (merged work stays). Refuses if uncommitted, unless --force."""
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    proj = s["projects"][s["epics"][t["epic"]]["project"]]
    problems = [("%s has uncommitted changes" % r) for r in t["repos"] if _changed(t["worktrees"][r])]
    if problems and not args.force:
        die("refusing — есть незакоммиченное:\n  - " + "\n  - ".join(problems) + "\n(--force чтобы убрать всё равно)")
    if t.get("pid") and pid_alive(t["pid"]):
        try:
            os.kill(int(t["pid"]), 15)
        except Exception:
            pass
    jira = proj.get("jira"); jkey = t.get("jira")
    if jira and jira.get("token") and jkey:
        if jira_status_category(jira, jkey) == "done":
            print("jira %s уже Done" % jkey)
        else:
            ok, info = jira_transition_done(jira, jkey)
            print(("jira %s -> Done (%s)" % (jkey, info)) if ok else ("jira %s не смог: %s" % (jkey, info)))
    task_dir = Path(next(iter(t["worktrees"].values()))).parent if t.get("worktrees") else None
    for r in t["repos"]:
        rp = proj["repos"].get(r, {}).get("path")
        wt = t["worktrees"].get(r)
        if rp and wt:
            git(["worktree", "remove", wt, "--force"], cwd=rp, check=False)
            git(["worktree", "prune"], cwd=rp, check=False)
            print("removed worktree: %s" % wt)
    if task_dir and task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)
        print("removed task dir: %s" % task_dir)
    s["tasks"].pop(args.task, None)
    save_state(s)
    print("task %s готово и убрано с доски (Jira->Done; remote MR/ветка не тронуты)." % args.task)

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
    """(url, state) for this branch's MR — PREFER an OPEN one, else the most-recent merged/closed.
    A branch can carry several MRs (e.g. one to an integration branch and one to master) and is often
    DELETED right after merge. `glab mr view <branch>` is unreliable here: it errors on a deleted
    branch AND is ambiguous when >1 MR shares the branch (it makes you pick an IID). So we go through
    the API by source_branch only: first ask for an OPEN one, then fall back to the most-recently
    updated MR of ANY state — which still resolves merged MRs after their branch is gone."""
    enc = remote.replace("/", "%2F")

    def _query(extra):
        out = run(["glab", "api",
                   "projects/%s/merge_requests?source_branch=%s&per_page=1&%s" % (enc, branch, extra)],
                  cwd=cwd, check=False)
        try:
            arr = json.loads(out.stdout or "[]")
        except Exception:
            arr = []
        return arr[0] if arr else None

    mr = _query("state=opened")                          # prefer an OPEN MR
    if not mr:
        mr = _query("order_by=updated_at&sort=desc")     # else most-recent merged/closed
    if mr:
        return (mr.get("web_url"), mr.get("state") or "?")
    return (None, None)

def open_mr(remote, branch, cwd):
    """The single OPEN MR whose source is `branch`, or None. Returns the raw dict (iid/web_url/
    target_branch/...). Used by merge commands — we only ever auto-merge an OPEN MR."""
    enc = remote.replace("/", "%2F")
    out = run(["glab", "api",
               "projects/%s/merge_requests?source_branch=%s&state=opened&per_page=1" % (enc, branch)],
              cwd=cwd, check=False)
    try:
        arr = json.loads(out.stdout or "[]")
    except Exception:
        arr = []
    return arr[0] if arr else None

def merge_mr(remote, iid, cwd, squash=False):
    """Merge MR <iid>. Returns (ok, msg). --auto-merge so a required-but-unfinished pipeline merges
    on green instead of failing; we do NOT remove the source branch (a follow-up fix may reuse it)."""
    cmd = ["glab", "mr", "merge", str(iid), "-R", remote, "--yes"]
    if squash:
        cmd.append("--squash")
    r = run(cmd, cwd=cwd, check=False)
    msg = ((r.stdout or "") + " " + (r.stderr or "")).strip().replace("\n", " ")
    return (r.returncode == 0, msg[:300])

def mr_url(out, remote, branch, cwd):
    # URL of the JUST-created MR — ONLY from the create output. Do NOT fall back to `glab mr view`
    # here: that returns a PRE-EXISTING (often merged) MR and masks a failed create. Empty == failed.
    text = (out.stdout or "") + "\n" + (out.stderr or "")
    m = re.search(r"https?://\S+/-/merge_requests/\d+", text)
    return m.group(0) if m else ""


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

_REASON_MARKERS = ("прежде чем", "в диф", "несоответств", "looking at", "let me", "i'll ", "i will ",
                   "here's", "here is", "note:", "wait,", "actually,", "review", "ревью", "анализ",
                   "проблема", "issue:", "however", "однако", "на самом деле", "по сути")

def _clean_subject(text, fallback, max_len=100):
    """Extract a clean SINGLE-LINE title from a model's output. Rejects leaked reasoning/review,
    multi-line/multi-sentence dumps, and over-long lines -> falls back to the task title. (cc adds
    the [IK-XXXX] prefix itself, so a leading ticket tag in the model output is stripped.)"""
    if not text:
        return fallback
    line = ""
    for raw in text.strip().splitlines():
        l = raw.strip().strip("`").strip()
        l = re.sub(r"^(mr title|title|subject)\s*[:\-]\s*", "", l, flags=re.I).strip().strip('"').strip()
        l = re.sub(r"^\[?IK-\d+\]?\s*[:\-]?\s*", "", l).strip()   # drop a leading ticket tag (cc re-adds it)
        if l:
            line = l; break
    if not line:
        return fallback
    low = line.lower()
    if len(line) > max_len or line.count(". ") >= 2 or any(m in low for m in _REASON_MARKERS):
        return fallback
    return line

def gen_commit_msg(wt, fallback):
    diff = git(["diff", "--cached"], cwd=wt, check=False).stdout or ""
    if not diff.strip():
        return fallback
    prompt = ("Write ONE git commit message for the STAGED DIFF below, following this repo's CLAUDE.md "
              "conventions (else Conventional Commits: type(scope): subject). HARD RULES: the subject is a "
              "SINGLE line (<=72 chars) describing ONLY what is in this diff — no review, no reasoning, no "
              "analysis, no preamble, nothing not present in the diff. Optionally a blank line then a short "
              "factual body. Output ONLY the message.\n\n=== staged diff ===\n" + diff[:12000])
    msg = claude_text(wt, prompt)
    if not msg:
        return fallback
    lines = msg.strip().splitlines()
    subject = _clean_subject(lines[0] if lines else "", fallback)
    body = "\n".join(lines[1:]).strip()
    return (subject + ("\n\n" + body if body and len(body) < 1500 else "")).strip()

def gen_task_title(prompt_text, cwd=None, fallback="task"):
    """A short imperative task title from the user's prompt/description (so you never have to write
    one). Uses headless claude; falls back to a trimmed first line of the prompt if unavailable."""
    p = (prompt_text or "").strip()
    if not p:
        return fallback
    fb = _clean_subject(p, fallback, max_len=60)   # first decent line of the prompt as the fallback
    ask = ("Write a SHORT task title for the work described below: 3-8 words, imperative mood, no "
           "trailing period, no ticket prefix, no quotes. Output ONLY the title.\n\n" + p[:2000])
    out = claude_text(cwd or tempfile.gettempdir(), ask, timeout=60)
    return _clean_subject(out or "", fb, max_len=70)

def gen_mr_text(wt, cmp_ref, fallback_title, fallback_desc):
    diff = git(["diff", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout or ""
    log = git(["log", "--oneline", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout or ""
    files = git(["diff", "--name-only", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout or ""
    if not diff.strip():
        return (fallback_title, fallback_desc)
    prompt = ("Summarize THIS branch's diff into a Merge Request, following the repo's CLAUDE.md.\n"
              "OUTPUT FORMAT (exactly): line 1 = the MR TITLE, then a line with only '---', then the "
              "description in markdown.\n"
              "TITLE RULES (strict): a SINGLE line, Conventional Commits `type(scope): subject` (<=72 "
              "chars). It MUST describe ONLY changes present in the diff/files below — do NOT mention "
              "anything not in this diff. NO review, NO reasoning, NO multi-line. Do NOT add a [IK-XXXX] "
              "prefix (it is added automatically). Put ALL analysis/notes in the DESCRIPTION, never the "
              "title.\n\n=== changed files ===\n" + files[:1500] + "\n=== commits ===\n" + log[:1500]
              + "\n=== diff ===\n" + diff[:14000])
    out = claude_text(wt, prompt)
    if not out:
        return (fallback_title, fallback_desc)
    if "---" in out:
        a, b = out.split("---", 1)
        return (_clean_subject(a, fallback_title), b.strip() or fallback_desc)
    lines = out.strip().splitlines()
    return (_clean_subject(lines[0] if lines else "", fallback_title),
            "\n".join(lines[1:]).strip() or fallback_desc)


def ensure_remote_target(wt, target, db):
    """Recreate the MR target (integration/release) branch on origin from the default branch if it
    doesn't exist. Supports the team flow: a release merges target -> master/main and DELETES the
    target branch; a follow-up task MR then RE-creates it. No-op if it already exists. Returns
    True if it (re)created the branch."""
    if run(["git", "ls-remote", "--heads", "origin", target], cwd=wt, check=False).stdout.strip():
        return False
    run(["git", "fetch", "origin", db], cwd=wt, check=False)
    base_ref = "origin/" + db if have_ref(wt, "origin/" + db) else db
    run(["git", "push", "origin", base_ref + ":refs/heads/" + target], cwd=wt, check=False)
    run(["git", "fetch", "origin", target], cwd=wt, check=False)   # refresh local origin/<target>
    return True

def cmd_task_mr(args):
    s = load_state()
    t = s["tasks"].get(args.task) or die("unknown task '%s'" % args.task)
    epic = s["epics"][t["epic"]]
    proj = s["projects"][epic["project"]]
    epic_mode = epic.get("mode") or ("targets" if epic.get("targets") else "epic_branch")
    tag = "" if epic.get("loose") else "[%s] " % t["epic"]   # epic-less tasks: no internal "[<key>__loose]" prefix in the MR title
    assignee = proj.get("default_assignee") or glab_user()
    any_real = False
    for r in t["repos"]:
        wt = t["worktrees"][r]
        ri = proj["repos"][r]
        if not ri.get("remote"):
            print("[%s] remote не задан — пропуск MR (`cc repo set %s %s --remote <slug>`)" % (r, epic["project"], r))
            continue
        target = t["base"][r]
        branch = t["branch"]
        db = default_branch(wt)
        target_live = bool(have_ref(wt, "origin/" + target))     # does the target branch still exist?
        cmp_base = target if target_live else db                 # if a release deleted it, compare vs master
        cmp_ref = ("origin/" + cmp_base) if have_ref(wt, "origin/" + cmp_base) else cmp_base
        ahead = run(["git", "rev-list", "--count", cmp_ref + "..HEAD"], cwd=wt, check=False).stdout.strip()
        if not _changed(wt) and ahead in ("", "0"):
            print("[%s] no changes vs %s - skipped" % (r, cmp_base))
            continue
        lead = ri.get("reviewer", "")
        title = tag + t["title"]
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
        elif not target_live:   # targets mode + a release deleted the integration branch -> recreate it
            if ensure_remote_target(wt, target, db):
                print("[%s] target-ветки '%s' не было (релиз её удалил?) — пересоздал из %s" % (r, target, db))
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
            mr_title = tag + ai_title
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
        if url:
            t["mrs"][r] = url
            print("[%s] MR -> %s" % (r, url))
        else:
            err = (out.stderr or out.stdout or "").strip().splitlines()
            print("[%s] ⚠️ MR НЕ создан: %s" % (r, (err[-1][:180] if err else "glab mr create не вернул URL")))
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
    have_any = made = 0
    for r, ri in proj["repos"].items():
        rp = ri["path"]
        # refresh the epic branch from the remote FIRST — a task MR merged into it lands on origin, and
        # a stale local origin/<key> would read 0-ahead (so `M` wrongly says "no changes"). No-op if absent.
        run(["git", "fetch", "origin", key], cwd=rp, check=False)
        if not (have_ref(rp, key) or have_ref(rp, "origin/" + key)):
            continue
        have_any += 1
        if not ri.get("remote"):
            print("[%s] remote не задан — пропуск (`cc repo set %s %s --remote <slug>`)" % (r, e["project"], r))
            continue
        db = default_branch(rp)
        run(["git", "fetch", "origin", db], cwd=rp, check=False)   # fresh base, so ahead-count is accurate
        # compare the REMOTE epic branch vs the REMOTE base — that's what the MR would contain. Tasks
        # merge into origin/<key>, while the local <key> in this checkout is often stale; _ahead_count
        # prefers the local branch, so use origin refs here explicitly.
        bref = ("origin/" + key) if have_ref(rp, "origin/" + key) else key
        baseref = ("origin/" + db) if have_ref(rp, "origin/" + db) else db
        _out = run(["git", "rev-list", "--count", baseref + ".." + bref], cwd=rp, check=False).stdout.strip()
        ahead = int(_out) if _out.isdigit() else 0
        if ahead == 0:
            # epic branch == master here: no task merged into it -> nothing to MR. Skip this repo.
            print("[%s] ветка эпика '%s' без изменений vs %s — пропуск (влей задачи в неё сначала)" % (r, key, db))
            continue
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
            print("[%s] DRY-RUN epic MR: %s -> %s  (%d commit(s) ahead, reviewer %s)" % (r, key, db, ahead, lead or "-"))
            print("    would: git push -u origin %s" % key)
            print("    would: " + " ".join(glab_cmd))
            made += 1
            continue
        push_epic_branch(rp, key)
        out = run(glab_cmd, cwd=rp, check=False)
        url = mr_url(out, ri["remote"], key, rp)
        e.setdefault("mrs", {})[r] = url
        print("[%s] epic MR -> %s  (%d commit(s))" % (r, url, ahead))
        made += 1
        audit("epic.mr", epic=args.key, repo=r, base=db, url=url)
    if made and not args.dry_run:
        save_state(s)
    if not have_any:
        print("(нет ветки эпика '%s' ни в одном репо — сначала создай задачу)" % key)
    elif made == 0:
        print("(во всех репо ветка эпика без изменений vs master — нечего вливать; сначала влей задачи в ветку эпика)")


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


def cmd_epic_plan(args):
    """Release plan: which repos of this epic to RELEASE vs SKIP. Source of truth for 'what to
    touch' — release/MR ONLY repos marked РЕЛИЗИТЬ; never touch a repo marked SKIP.

    The signal is the repo's epic->master MR state (same detection as `cc epic mrs`), NOT a local
    git diff: origins are often forks whose master is thousands of commits behind canonical, so
    `origin/master..branch` lies. MR state is reliable across that:
      - OPEN epic->master MR  -> РЕЛИЗИТЬ (merge it / it's the release MR)
      - MERGED                -> SKIP (already released)
      - no MR, but branch ahead of master (git, weak) -> "изменения есть, MR в master нет" (релизить)
      - no MR, nothing ahead / no branch -> SKIP"""
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    mode = e.get("mode") or ("targets" if e.get("targets") else "epic_branch")
    bm = _epic_branch_map(e, proj)
    repos = e.get("repos") or list(proj["repos"].keys())
    changed, skip = [], []
    print("epic %s — release plan (mode=%s, по состоянию MR в master):" % (args.key, mode))
    for r in repos:
        ri = proj["repos"].get(r)
        if not ri or not ri.get("remote"):
            print("  [%s] remote не задан                              → SKIP" % r); skip.append(r); continue
        br = bm.get(r)
        url, state, db = _epic_master_mr(ri, br or args.key, args.key)   # NETWORK, no lock held
        if state == "opened":
            print("  [%s] открытый MR → %s: %s              → РЕЛИЗИТЬ" % (r, db, url)); changed.append(r)
        elif state == "merged":
            print("  [%s] MR уже влит в %s                            → SKIP" % (r, db)); skip.append(r)
        else:
            ahead = _ahead_count(ri["path"], br, db) if br else 0
            if ahead and br and (have_ref(ri["path"], "origin/" + br) or have_ref(ri["path"], br)):
                print("  [%s] '%s' впереди %s, MR в master нет           → РЕЛИЗИТЬ (создать MR)" % (r, br, db))
                changed.append(r)
            else:
                print("  [%s] изменений в %s нет                          → SKIP" % (r, db)); skip.append(r)
    print("\nРелизить ТОЛЬКО: %s" % (", ".join(changed) or "(нет изменений ни в одном репо — релизить нечего)"))
    if skip:
        print("Не трогать:      %s" % ", ".join(skip))


def cmd_epic_merge(args):
    # self-locked: glab merges are slow network; don't hold the global lock.
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    tids = [tid for tid, t in s["tasks"].items() if t.get("epic") == args.key]
    if not tids:
        print("(в эпике %s нет задач)" % args.key); return
    print("merge epic %s — открытые MR ВСЕХ задач (task → integration; это НЕ релиз в master)%s" % (
        args.key, "  [DRY-RUN]" if args.dry_run else ""))
    total = {"merged": 0, "failed": 0, "skipped": 0}
    touched = []
    for tid in tids:
        t = s["tasks"][tid]
        if t.get("merged"):
            print("[%s] уже влита — skip" % tid); continue
        print("[%s] %s" % (tid, t.get("title", "")))
        res = _task_merge_pass(t, proj, args.dry_run, args.squash, tid=tid)
        for k in total:
            total[k] += res[k]
        if not args.dry_run and res["merged"]:
            touched.append(tid)
    print("\nИТОГО: %d merged, %d failed, %d skipped" % (total["merged"], total["failed"], total["skipped"]))
    if touched:
        print("refreshing MR state …")
        for tid in touched:
            _refresh_task(tid)


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


def _jira_done_keys(jira, keys):
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

def _epic_teardown(s, proj, key):
    """Remove an epic + its tasks from cc LOCALLY (worktrees, branches, dirs, state). Remote
    MRs/branches untouched. Returns the list of removed task ids.
    Before removal we STASH the epic's knowledge (summary/notes/mode/targets) into
    state['epic_knowledge'] so that re-adding the same epic later (`cc epic add <KEY>`) brings
    its accumulated context back — the board clears, the knowledge doesn't."""
    e = s["epics"].get(key)
    if e:
        s.setdefault("epic_knowledge", {})[key] = {
            "project": e.get("project"), "summary": e.get("summary", ""),
            "memory": e.get("memory", ""), "mode": e.get("mode"),
            "targets": e.get("targets", {}) or {}, "done_at": time.strftime("%Y-%m-%d")}
    tasks = [t for t, v in s["tasks"].items() if v.get("epic") == key]
    for tid in tasks:
        t = s["tasks"][tid]
        dirty = [r for r in t.get("repos", []) if _changed(t.get("worktrees", {}).get(r, ""))]
        if dirty:
            print("  ! task %s незакоммичено в: %s (убираю всё равно)" % (tid, ", ".join(dirty)))
        task_dir = Path(next(iter(t["worktrees"].values()))).parent if t.get("worktrees") else None
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
        print("  removed task %s" % tid)
    epic_dir = Path(proj.get("path", "")) / "cctui" / key
    try:
        if epic_dir.exists() and not any(epic_dir.iterdir()):
            epic_dir.rmdir()
    except Exception:
        pass
    s["epics"].pop(key, None)
    return tasks

def cmd_epic_done(args):
    """Готово и убрать с доски: Jira(epic + children/linked)->Done, then remove the epic and its
    tasks from cc locally. Remote MRs/branches are NOT touched (merged work stays)."""
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    jira = proj.get("jira")
    if jira and jira.get("token") and args.key.split("-")[0] == jira.get("project_key"):
        try:
            children = [c["key"] for c in jira_epic_children(jira, args.key)]
        except Exception:
            children = []
        local = [t.get("jira") for t in s["tasks"].values()
                 if t.get("epic") == args.key and t.get("jira")]
        keys = list(dict.fromkeys([args.key] + children + local))
        _jira_done_keys(jira, keys)
        mem = (e.get("memory") or "").strip()
        if mem and args.key.split("-")[0] == jira.get("project_key"):
            ok = jira_comment(jira, args.key, "cc — накопленные знания по эпику (мудрость фичи):\n\n" + mem)
            print("  jira: заметки эпика %s" % ("сохранены комментарием" if ok else "не удалось отправить"))
    removed = _epic_teardown(s, proj, args.key)
    save_state(s)
    print("epic %s готово и убрано с доски%s (remote MR/ветки не тронуты)." % (
        args.key, (" + %d задач(и)" % len(removed)) if removed else ""))

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
    _jira_done_keys(jira, [args.key] + children)

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
    _epic_teardown(s, proj, args.key)
    save_state(s)
    print("epic %s removed from cc%s (Jira + remote MRs/branches NOT touched)" % (
        args.key, (" + %d task(s) incl. their worktrees" % len(tasks)) if tasks else ""))


RELEASE_RUNBOOK = """# Epic %s: %s — release & coordination chat

You drive RELEASES and cross-repo coordination for THIS epic. The repos are added with
--add-dir (their MAIN checkouts). cc does NOT own git here — you run git/glab/eas yourself.

## Release levels (the model)
task -> epic -> prod. A TASK's MR lands its work INTO the epic branch (merging a task is NOT a prod
release). The EPIC is the prod RELEASE unit: once the needed task MRs are in the epic branch, you MR
the epic branch -> master/main and deploy. Merge task MRs with `cc epic merge <KEY>` (all) or
`cc task merge <tid>` (one); the user reviews the MRs and you merge on their word — never to master
without an explicit OK.

## Repos & integration branches (release source / MR target)
%s

## Epic notes
%s

## STEP 0 — which repos actually changed (DO THIS FIRST, always)
Run `cc epic plan %s`. It marks each repo РЕЛИЗИТЬ (its integration/epic branch is ahead of
master) or SKIP (no changes). **Release / MR ONLY the РЕЛИЗИТЬ repos. NEVER create an MR, bump a
version, tag, or deploy a repo marked SKIP** — even if it's listed under the epic. The repo list
above is just membership; `cc epic plan` is the source of truth for what to touch. Run `git fetch`
in a repo first if you suspect its local refs are stale.

## Merging reviewed task work (when asked to "merge tasks", not "release")
The user reviews on the board, then asks you to merge — do NOT open each MR by hand:
- One task:   `cc task merge <tid>`        (merges that task's OPEN MRs, per repo)
- All tasks:  `cc epic merge %s`           (merges every task's OPEN MRs)
- Preview first with `--dry-run`; add `--squash` if the repo wants squashed history.
These merge task branches INTO their integration/target branch (NOT into master) and skip any repo
with no open MR. This is "land the reviewed work"; the prod release below is a separate, later step.

## Release runbook (when asked to release this epic)
Per repo — **only repos `cc epic plan` marked РЕЛИЗИТЬ** (NEVER disturb the user's working checkout —
use a temp `git worktree` off the integration branch for the version bump):
1. Confirm the integration branch is green on stage (CI) before releasing.
2. ACTUALIZE with prod FIRST: `git fetch origin <default>` then merge `origin/<default>` INTO the
   integration branch and resolve conflicts — so you release ON TOP of the current master/main, never a
   stale base. (Sanity: `git rev-list --count origin/<default>..<branch>` should be a small,
   release-sized number — if it's thousands, the branch is on the wrong/unrelated base, STOP.) Re-run
   stage CI after the merge if the base moved.
3. Bump to a genuinely NEW version on a release branch off the (now-actualized) integration branch:
   read the current version (package.json / equivalent) AND `git tag --sort=-v:refname | head`,
   increment it (patch for fixes, minor for features) — NEVER reuse an existing version, and verify the
   target tag `vX.Y.Z` does NOT already exist before tagging. Commit the bump.
4. MR integration -> main (reviewer = area lead); merge once the pipeline is green.
5. Tag the NEW vX.Y.Z on main -> the tag pipeline runs the prod jobs.
6. Play/await `Containerize Prod` + `Deploy ECS Prod` (GitLab), or `eas update --branch production` (Expo/mobile).
7. New website routes (e.g. /loyalty): POST build-website-routes so they register on prod.
8. Close the epic's Jira tasks + the epic -> Done.

## Release train & safety (read before prod)
- Backend has no API versioning: web/admin must not hit prod before backend+mobile. Release
  the train together; verify the backend prod actually has the endpoints the frontend calls.
- Feature-flag-gated surfaces ship DARK; flip the flag only after the backend is live on prod.
- glab: use canonical `invictusfitness/*` paths (moved forks return 405 on POST). eas for mobile.
- Target/integration branches are per-release-window & EPHEMERAL: after MR'ing one into master/main,
  DELETING it on the remote is the team's normal practice. A follow-up fix on a task RE-creates the
  target — `cc task mr` recreates a missing target branch from the default branch and re-MRs the task
  INTO it (don't MR straight to master). When releasing: merge each target -> master/main, then it's
  fine to delete the target branch.
- Full autonomy through prod is authorized for this chat — still narrate every step and the result.
"""

# ----------------------------- ops agents (test / stage / deploy) -----------------------------
# A control-panel action does NOT hard-code per-project commands. It launches a headless claude
# (an "ops agent") scoped to the group, with a generic runbook telling it to STUDY the repos and
# figure out how to run/deploy them itself, report each step, and ask via [cc-needs-input] when
# unsure. cc owns scope + runbook + visibility (log/audit/[cc-needs-input]); the agent owns the
# project-specific knowledge. See memory: cc-orchestrate-not-hardcode.

OPS_KINDS = {
    "test": ("LOCAL TEST", (
        "Get this group's work RUNNING locally so the user can test it.\n"
        "1. STUDY each repo: read its CLAUDE.md, package.json scripts, README, .env.example, any\n"
        "   docker-compose/Procfile, and CI config — figure out how to run it.\n"
        "2. Run the repos TOGETHER locally (backends first, then frontend). Swap env so the frontend\n"
        "   points at the LOCAL backend — find the right URL/port keys in the .env files yourself.\n"
        "3. Smoke-check it's up (curl the health endpoint / hit the port) and report what you see.\n"
        "- This is LOCAL only. Do NOT deploy anything.")),
    "stage": ("DEPLOY TO STAGE", (
        "Deploy this group's merged work to STAGING.\n"
        "1. STUDY how each repo ships to stage: read CLAUDE.md, CI config (.gitlab-ci.yml / eas.json),\n"
        "   deploy scripts. Figure out the exact stage step (which CI job / which command).\n"
        "2. Trigger the stage deploy for each repo of the group. Report each step + the result/URL.\n"
        "- STAGE only — never touch production. If a repo has no stage path you can find, ask.")),
    "deploy": ("DEPLOY (per project's release process)", (
        "Deploy this group per the project's own release process.\n"
        "1. STUDY each repo's release/deploy docs (CLAUDE.md, CI, scripts).\n"
        "2. Follow that process, respecting the release train. Report each step.\n"
        "- Confirm the target (stage vs prod) before any prod action via [cc-needs-input].")),
}

def _group_worktrees(s, ekey):
    """[(repo, worktree, branch)] across all tasks of the group — where the work physically lives."""
    out = []
    for tid, t in s["tasks"].items():
        if t.get("epic") != ekey:
            continue
        for r, wt in (t.get("worktrees") or {}).items():
            out.append((r, wt, t.get("branch", "?")))
    return out

def render_ops_runbook(s, ekey, kind):
    """The ops agent's CLAUDE.md — generic per-kind rules + the group's repo/worktree map. The agent
    studies the repos itself; cc never bakes in per-project commands."""
    label, body = OPS_KINDS[kind]
    wts = _group_worktrees(s, ekey)
    repo_map = "\n".join("- %s  (branch %s)  ->  %s" % (r, br, wt) for r, wt, br in wts) or "(нет worktrees)"
    return (
        "# cc OPS agent — %s — group %s\n\n"
        "You are a cc OPS agent. cc gives you the scope + rules; YOU figure out the project specifics.\n\n"
        "## Group repos & worktrees (the work lives here — touch ONLY these):\n%s\n\n"
        "## Your job\n%s\n\n"
        "## Rules\n"
        "- Do NOT run git — cc owns branches/commits/MRs. Edit only inside the worktrees above.\n"
        "- You run NON-INTERACTIVELY in the background: NEVER call AskUserQuestion, never wait. Do\n"
        "  everything you safely can first. If you genuinely cannot proceed (don't know how to run a\n"
        "  repo, missing a secret/port, ambiguous which services, or a prod action needs the user's\n"
        "  ok) — make the VERY LAST line of your output exactly:\n"
        "  `[cc-needs-input] <one-line question + your recommended default>`  (nothing after it).\n"
        "  The user sees ❓ on the board and answers when they open this chat.\n"
        "- REPORT each step you take and its result, clearly, as you go.\n"
    ) % (label, ekey, repo_map, body)

def ops_id(ekey, kind):
    return "ops_%s_%s" % (kind, slugify(ekey))

def cmd_epic_ops(args):
    """Launch a headless ops agent (test/stage/deploy) for a group. cc prepares the scope + runbook
    and runs claude in the background; the agent studies the repos and acts, logging visibly and
    asking via [cc-needs-input]. NOT per-project hard-coded — the agent owns the how."""
    kind = args.kind
    if kind not in OPS_KINDS:
        die("kind должен быть один из: %s" % ", ".join(OPS_KINDS))
    s = load_state()
    e = s["epics"].get(args.key) or die("unknown epic '%s'" % args.key)
    proj = s["projects"][e["project"]]
    wts = _group_worktrees(s, args.key)
    if not wts:
        die("в группе %s нет worktrees (создай/проведи задачи сначала)" % args.key)
    opsdir = Path(proj["path"]) / "cctui" / args.key / ("_ops-" + kind)
    opsdir.mkdir(parents=True, exist_ok=True)
    (opsdir / "CLAUDE.md").write_text(render_ops_runbook(s, args.key, kind))
    oid = ops_id(args.key, kind)
    log = STATE_DIR / ("%s.log" % oid)
    adds = [wt for _, wt, _ in wts]
    s.setdefault("ops", {})[oid] = {"epic": args.key, "kind": kind, "dir": str(opsdir),
                                    "log": str(log), "status": "running", "claude_session": {}}
    save_state(s)
    audit("ops.start", epic=args.key, kind=kind)
    if getattr(args, "manual", False):
        print("ops dir готов (manual, агент не запущен): %s" % opsdir)
        return
    prompt = ("[cc] You are the OPS agent for group %s (%s). Follow this folder's CLAUDE.md: study the "
              "repos, do the job, report each step, and if you must stop, end with a single "
              "`[cc-needs-input] …` line." % (args.key, kind))
    addflags = []
    for a in adds:
        addflags += ["--add-dir", a]
    with open(log, "w") as lf:
        proc = subprocess.Popen(["claude", "--permission-mode", "bypassPermissions", "-p", prompt] + addflags,
                                cwd=str(opsdir), stdout=lf, stderr=lf)
    s = load_state(); s["ops"][oid]["pid"] = proc.pid; save_state(s)
    print("ops-агент [%s] запущен для группы %s (pid=%s)\n  лог: %s\n  следить: cc tui (или tail -f лог)"
          % (kind, args.key, proc.pid, log))

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
    lines.append("(membership only — run `cc epic plan %s` for which repos actually changed)" % args.key)
    runbook = RELEASE_RUNBOOK % (args.key, e.get("summary", ""), "\n".join(lines),
                                 (e.get("memory") or "").strip() or "(no epic notes)",
                                 args.key, args.key)
    runbook += jira_chat_setup(edir, proj, e["project"])   # project-scoped Jira + block the MCP
    (edir / "CLAUDE.md").write_text(runbook)
    adds = " ".join("--add-dir %s" % shlex.quote(proj["repos"][r]["path"])
                    for r in repos if proj["repos"].get(r, {}).get("path"))
    print("epic chat dir: %s" % edir)
    print("  release runbook CLAUDE.md written (%d repo(s))" % len(repos))
    print("  open:  cd %s && claude --permission-mode auto %s%s" % (
        shlex.quote(str(edir)), adds, chat_jira_flags(proj, e["project"])))


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
    if getattr(args, "remote", None) is not None:
        r["remote"] = args.remote
        if args.remote:
            r["provider"] = ("gitlab" if "gitlab" in args.remote
                            else "github" if "github" in args.remote else r.get("provider", "unknown"))
    if getattr(args, "default_branch", None):
        r["default_branch"] = args.default_branch
    save_state(s)
    audit("repo.set", project=args.project, repo=args.repo,
          default_branch=(args.default_branch if getattr(args, "default_branch", None) else None),
          remote=(args.remote if getattr(args, "remote", None) is not None else None))
    print("%s/%s  remote=%r  default_branch=%r  run=%r  reviewer=%r" % (
        args.project, args.repo, r.get("remote", ""), r.get("default_branch", ""), r.get("run", ""), r.get("reviewer", "")))

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

def jira_attachments(cfg, key):
    """[{id, filename, mime, size, content(url)}] for an issue's attachments."""
    f = jira_req(cfg, "GET", "/issue/%s?fields=attachment" % key).get("fields", {})
    out = []
    for a in (f.get("attachment") or []):
        out.append({"id": a.get("id"), "filename": a.get("filename", "") or ("attachment-%s" % a.get("id")),
                    "mime": a.get("mimeType", "") or "", "size": int(a.get("size") or 0),
                    "content": a.get("content", "")})
    return out

def jira_download(cfg, url, dest):
    """Download an attachment to `dest` under the project token. Jira's content URL 303-redirects to a
    pre-signed media URL; urllib follows it (and strips the auth header cross-origin, which the signed
    URL doesn't need). Returns bytes written."""
    req = urllib.request.Request(url, method="GET")
    auth = base64.b64encode(("%s:%s" % (cfg["email"], cfg["token"])).encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)

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

def jira_project_tasks(cfg, query="", limit=40):
    """ANY non-Epic issue in the project (regardless of its parent epic), recent first — for pulling
    an EXISTING ticket onto the board as a task. [{key,summary,status,done,parent}]."""
    jql = "project = %s AND issuetype != Epic" % cfg["project_key"]
    if query:
        jql += ' AND summary ~ "%s"' % query.replace('"', "")
    jql += " ORDER BY updated DESC"
    body = {"jql": jql, "maxResults": limit, "fields": ["summary", "status", "parent"]}
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
                    "parent": (f.get("parent") or {}).get("key")})
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
        nm = (t.get("to") or {}).get("name", "Done")
        audit("jira.transition", key=key, to=nm)
        return (True, nm)
    except Exception as e:
        return (False, str(e)[:50])

def jira_adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": (text or " ")[:4000]}]}]}

def jira_comment(cfg, key, text):
    try:
        jira_req(cfg, "POST", "/issue/%s/comment" % key, {"body": jira_adf(text)})
        return True
    except Exception:
        return False

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


def _jira_cfg(s, project):
    """The project's Jira config (token), or die. The agent-facing `cc jira` commands always go
    through THIS — a project's own Jira — never an MCP that might point at a different instance."""
    p = s["projects"].get(project) or die("unknown project '%s'" % project)
    cfg = p.get("jira") or {}
    if not (cfg.get("token") and cfg.get("site") and cfg.get("project_key")):
        die("project '%s' has no Jira token (set it: cc project jira %s --site .. --email .. --token .. --project-key ..)"
            % (project, project))
    return cfg

def cmd_jira_search(args):
    cfg = _jira_cfg(load_state(), args.project)
    if getattr(args, "jql", None):
        body = {"jql": args.jql, "maxResults": 40, "fields": ["summary", "status", "parent"]}
        try:
            data = jira_req(cfg, "POST", "/search/jql", body)
        except Exception:
            data = jira_req(cfg, "POST", "/search", body)
        rows = [{"key": i["key"], "summary": (i.get("fields", {}) or {}).get("summary", ""),
                 "status": ((i.get("fields", {}) or {}).get("status") or {}).get("name", ""),
                 "parent": ((i.get("fields", {}) or {}).get("parent") or {}).get("key")}
                for i in data.get("issues", [])]
    else:
        rows = jira_project_tasks(cfg, args.query or "")
    for t in rows:
        par = ("  · " + t["parent"]) if t.get("parent") else ""
        print("%s  [%s]%s  %s" % (t["key"], t.get("status", ""), par, t.get("summary", "")))
    if not rows:
        print("(ничего не найдено в %s)" % cfg["project_key"])

def cmd_jira_get(args):
    cfg = _jira_cfg(load_state(), args.project)
    try:
        f = jira_req(cfg, "GET", "/issue/%s?fields=summary,status,parent" % args.key).get("fields", {})
    except Exception as ex:
        die("jira get %s failed: %s" % (args.key, str(ex)[:120]))
    st = (f.get("status") or {}).get("name", "")
    par = (f.get("parent") or {}).get("key")
    print("%s  [%s]%s" % (args.key, st, ("  parent=" + par) if par else ""))
    print(f.get("summary", ""))
    desc = jira_epic_description(cfg, args.key)
    if desc:
        print("\n" + desc)

def cmd_jira_comment(args):
    cfg = _jira_cfg(load_state(), args.project)
    print(("commented on %s" % args.key) if jira_comment(cfg, args.key, args.text)
          else ("comment on %s failed" % args.key))

def cmd_jira_done(args):
    cfg = _jira_cfg(load_state(), args.project)
    ok, info = jira_transition_done(cfg, args.key)
    print(("%s -> %s" % (args.key, info)) if ok else ("could not transition %s: %s" % (args.key, info)))

def jira_transitions(cfg, key):
    """Available transitions from the issue's CURRENT status: [{id, name, to_name, to_cat}]."""
    trs = jira_req(cfg, "GET", "/issue/%s/transitions" % key).get("transitions", [])
    out = []
    for t in trs:
        to = t.get("to") or {}
        out.append({"id": t.get("id"), "name": t.get("name", ""), "to_name": to.get("name", ""),
                    "to_cat": ((to.get("statusCategory") or {}).get("key") or "").lower()})
    return out

# keyword -> (statusCategory, name-tokens). A keyword matches a transition ONLY if its target status
# is in that category AND its NAME contains one of the tokens. The name requirement matters because a
# project may MIScategorize statuses (visco puts "Готово к проверке"/"in test" in `new`); matching by
# category alone could silently move an issue to a semantically-wrong status. Use an exact name to be sure.
_KW = {
    "todo": ("new", ("к выполнению", "to do", "todo", "backlog", "новая")),
    "to do": ("new", ("к выполнению", "to do", "todo", "backlog", "новая")),
    "к выполнению": ("new", ("к выполнению", "to do", "todo", "backlog", "новая")),
    "backlog": ("new", ("backlog", "бэклог", "к выполнению")),
    "doing": ("indeterminate", ("в работе", "in progress", "doing", "progress", "wip")),
    "in progress": ("indeterminate", ("в работе", "in progress", "doing", "progress", "wip")),
    "в работе": ("indeterminate", ("в работе", "in progress", "doing", "progress", "wip")),
    "wip": ("indeterminate", ("в работе", "in progress", "doing", "progress", "wip")),
    "done": ("done", ("готово", "done", "complete", "выполнено", "closed", "завершен")),
    "готово": ("done", ("готово", "done", "complete", "выполнено", "closed", "завершен")),
    "complete": ("done", ("готово", "done", "complete", "выполнено", "closed", "завершен")),
}

def jira_move(cfg, key, target):
    """Transition issue to `target` — a status NAME (case-insensitive, EXACT) or a keyword
    (todo/doing/done/…). Only transitions AVAILABLE from the current status are considered; backward
    moves work iff the workflow allows them. EXACT NAME is deterministic and always preferred. A
    keyword resolves to a transition whose target is in the keyword's category AND whose NAME matches
    a token — never by category alone (a miscategorized status would be a footgun). If a keyword/
    substring matches 0 or >1 transitions, we REFUSE and ask for an exact name. (ok, info)."""
    trs = jira_transitions(cfg, key)
    if not trs:
        return (False, "нет доступных переходов")
    tl = (target or "").strip().lower()
    avail = ", ".join(t["to_name"] for t in trs)
    pick = next((t for t in trs if t["to_name"].strip().lower() == tl), None)   # 1) exact status name
    if not pick:
        kw = _KW.get(tl)
        if kw:
            catk, tokens = kw                                                   # 2) keyword: category + name token
            cands = [t for t in trs if t["to_cat"] == catk
                     and any(tok in t["to_name"].lower() for tok in tokens)]
        else:
            cands = [t for t in trs if tl and tl in t["to_name"].lower()]       # 3) name substring
        if len(cands) > 1:
            return (False, "'%s' неоднозначно — подходят: %s. Укажи точное имя статуса."
                    % (target, ", ".join(t["to_name"] for t in cands)))
        pick = cands[0] if cands else None
    if not pick:
        return (False, "нет перехода в '%s' из текущего статуса (доступно: %s)" % (target, avail))
    try:
        jira_req(cfg, "POST", "/issue/%s/transitions" % key, {"transition": {"id": pick["id"]}})
        audit("jira.transition", key=key, to=pick["to_name"])
        return (True, pick["to_name"])
    except Exception as e:
        return (False, str(e)[:100])

def cmd_jira_transitions(args):
    cfg = _jira_cfg(load_state(), args.project)
    cur = ((jira_req(cfg, "GET", "/issue/%s?fields=status" % args.key).get("fields", {}) or {})
           .get("status") or {}).get("name", "?")
    print("%s сейчас: %s\nможно перейти в:" % (args.key, cur))
    for t in jira_transitions(cfg, args.key):
        print("  %-24s [%s]" % (t["to_name"], t["to_cat"]))

def cmd_jira_move(args):
    cfg = _jira_cfg(load_state(), args.project)
    ok, info = jira_move(cfg, args.key, args.status)
    print(("%s -> %s ✓" % (args.key, info)) if ok else ("%s: %s" % (args.key, info)))

def cmd_jira_rollup(args):
    """Actualize the board: a parent in a DONE status that still has unfinished children is moved
    back to a not-done status (default 'К выполнению') so it reflects reality. Preview by default;
    pass --apply to actually move. Scope: --key <parent> (any level), else all project epics."""
    cfg = _jira_cfg(load_state(), args.project)
    to = args.to or "К выполнению"
    parents = [{"key": args.key}] if args.key else jira_my_epics(cfg)
    flagged = []
    for p in parents:
        pk = p["key"]
        f = jira_req(cfg, "GET", "/issue/%s?fields=status,summary" % pk).get("fields", {})
        pst = (f.get("status") or {}).get("name", "")
        pcat = ((f.get("status") or {}).get("statusCategory") or {}).get("key", "")
        kids = jira_epic_children(cfg, pk)
        undone = [k for k in kids if not k.get("done")]
        if not kids or pcat != "done" or not undone:
            continue
        flagged.append(pk)
        print("[%s] '%s' — %s, но %d/%d детей не завершены → %s"
              % (pk, (f.get("summary", "") or "")[:48], pst, len(undone), len(kids), to))
        for k in undone[:8]:
            print("      • %s [%s] %s" % (k["key"], k["status"], (k["summary"] or "")[:50]))
        if args.apply:
            ok, info = jira_move(cfg, pk, to)
            print("      %s" % ("→ %s ✓" % info if ok else "не удалось: %s" % info))
    if not flagged:
        print("актуально ✓ — нет родителей в «Готово» с незавершёнными детьми")
    elif not args.apply:
        print("\n[превью] сдвинул бы %d родителей. Добавь --apply чтобы применить." % len(flagged))

_IMG_MIME = ("image/",)
def cmd_jira_attachments(args):
    cfg = _jira_cfg(load_state(), args.project)
    atts = jira_attachments(cfg, args.key)
    if not atts:
        print("(у %s нет вложений)" % args.key); return
    for a in atts:
        print("%-40s %-18s %8d B  id=%s" % (a["filename"][:40], a["mime"], a["size"], a["id"]))
    print("\nскачать: cc jira pull %s %s [--out <dir>] [--images-only]" % (args.project, args.key))

def cmd_jira_pull(args):
    cfg = _jira_cfg(load_state(), args.project)
    atts = jira_attachments(cfg, args.key)
    if getattr(args, "images_only", False):
        atts = [a for a in atts if a["mime"].startswith(_IMG_MIME)]
    if not atts:
        print("(нечего качать)"); return
    out = Path(args.out).expanduser() if getattr(args, "out", None) else (Path.cwd() / "jira-attachments" / args.key)
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for a in atts:
        name = os.path.basename(a["filename"]) or ("attachment-%s" % a["id"])
        dest = out / name
        n = 2
        while dest.exists():                       # don't clobber same-named attachments
            dest = out / ("%s-%d%s" % (dest.stem, n, dest.suffix)); n += 1
        try:
            sz = jira_download(cfg, a["content"], str(dest))
            kind = "image" if a["mime"].startswith(_IMG_MIME) else ("pdf" if "pdf" in a["mime"] else ("video" if a["mime"].startswith("video/") else "file"))
            print("[%s] %s  (%d B)  %s" % (kind, dest, sz, "← Read это, агент" if kind in ("image", "pdf") else ""))
            ok += 1
        except Exception as ex:
            print("[FAIL] %s: %s" % (name, str(ex)[:100]))
    print("\nскачано %d/%d в %s" % (ok, len(atts), out))
    if any(a["mime"].startswith("video/") for a in atts):
        print("видео скачано, но кадры агент не «видит» — нужен ffmpeg для извлечения кадров")

ATLASSIAN_TOOLS = "mcp__claude_ai_Atlassian__*"

def chat_jira_flags(proj, project_name):
    """Launch-time flags for a cc chat so it uses the PROJECT's Jira (token), not the Atlassian MCP —
    works regardless of cwd (unlike a .claude/settings.json, which only applies from cwd downward and
    whose `deniedMcpServers` can't even name a connector with a space/dot). Empirically `--disallowedTools
    'mcp__claude_ai_Atlassian__*'` blocks ONLY the Atlassian connector's tool calls (other connectors —
    Figma/DWH/… — stay). We also append a short system-prompt pointer in case the chat lands in the wrong
    cwd and never loads the CLAUDE.md block. Returns "" when the project has no token."""
    cfg = (proj.get("jira") or {})
    if not (cfg.get("token") and cfg.get("project_key")):
        return ""
    hint = ("Jira for project '%s' is %s (key %s). ALWAYS use the cc CLI for Jira — `cc jira "
            "search|get|comment|done|attachments|pull %s …` — it targets THIS project's Jira. The "
            "Atlassian MCP is intentionally blocked in this session; do not attempt to use it." % (
            project_name, cfg.get("site", "?"), cfg.get("project_key", "?"), project_name))
    return " --disallowedTools %s --append-system-prompt %s" % (shlex.quote(ATLASSIAN_TOOLS), shlex.quote(hint))

def _clean_dead_settings(chat_dir):
    """Remove the dead `deniedMcpServers` key an earlier version wrote into a chat's
    .claude/settings.json (Claude Code rejects it: 'expected object, received string'). Delete the
    file if it becomes empty; leave any other keys intact."""
    sf = Path(chat_dir) / ".claude" / "settings.json"
    if not sf.exists():
        return
    try:
        data = json.loads(sf.read_text())
    except Exception:
        return
    if not isinstance(data, dict) or "deniedMcpServers" not in data:
        return
    data.pop("deniedMcpServers", None)
    try:
        if data:
            sf.write_text(json.dumps(data, indent=2))
        else:
            sf.unlink()
    except Exception:
        pass

def jira_chat_setup(chat_dir, proj, project_name):
    """Markdown block for a chat's CLAUDE.md telling the agent to use `cc jira <project> …` and that the
    Atlassian MCP is blocked. The HARD block + a cwd-independent pointer come from chat_jira_flags() on the
    launch command; this block is the fuller, in-context version (loads when cwd is correct). "" if no token.
    Also self-heals: an earlier version wrote a `.claude/settings.json` with `deniedMcpServers:
    ["claude.ai Atlassian"]`, which Claude Code rejects on every launch ("expected object, received
    string") — strip that dead key here (and delete the file if it becomes empty)."""
    _clean_dead_settings(chat_dir)
    cfg = (proj.get("jira") or {})
    if not (cfg.get("token") and cfg.get("project_key")):
        return ""
    return ("\n## Jira — use cc, NOT the Atlassian MCP\n"
            "This project's Jira is **%s** (project key **%s**), wired into cc via token. For EVERY Jira\n"
            "action use the cc CLI — it always targets THIS project's Jira:\n"
            "- `cc jira search %s \"<text>\"`  (or `--jql \"<JQL>\"`)\n"
            "- `cc jira get %s <KEY>`  ·  `cc jira comment %s <KEY> \"<text>\"`  ·  `cc jira done %s <KEY>`\n"
            "- `cc jira move %s <KEY> \"<status>\"` — set ANY status (fwd OR back, e.g. \"К выполнению\"); "
            "`cc jira transitions %s <KEY>` lists what's reachable. (To change a status, ALWAYS use this — "
            "never raw REST.)\n"
            "- `cc jira rollup %s [--key <KEY>] [--apply]` — actualize the board: a parent in Done with\n"
            "  unfinished children is moved back to «К выполнению» (preview without --apply).\n"
            "- `cc jira attachments %s <KEY>` then `cc jira pull %s <KEY>` — download attachments (designs,\n"
            "  screenshots, repro files) locally, then Read the printed image/PDF paths for task context.\n"
            "Do NOT use the Atlassian MCP here — it points to a DIFFERENT Atlassian instance and is blocked\n"
            "at launch (--disallowedTools).\n") % (
            cfg.get("site", "?"), cfg.get("project_key", "?"),
            project_name, project_name, project_name, project_name, project_name,
            project_name, project_name, project_name, project_name)


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
    a = pj.add_parser("new"); a.add_argument("name"); a.add_argument("path", nargs="?"); a.set_defaults(fn=cmd_project_new)
    a = pj.add_parser("setup"); a.add_argument("project"); a.set_defaults(fn=cmd_project_setup)
    a = pj.add_parser("target"); a.add_argument("project"); a.add_argument("spec", nargs="*"); a.add_argument("--clear", action="store_true"); a.set_defaults(fn=cmd_project_target)
    pj.add_parser("ls").set_defaults(fn=cmd_project_ls)
    a = pj.add_parser("jira"); a.add_argument("project")
    a.add_argument("--site"); a.add_argument("--email"); a.add_argument("--token")
    a.add_argument("--project-key", dest="project_key"); a.add_argument("--off", action="store_true")
    a.set_defaults(fn=cmd_project_jira)

    a = sub.add_parser("deploys"); a.add_argument("project"); a.add_argument("repo", nargs="?")
    a.set_defaults(fn=cmd_deploys)
    sub.add_parser("orphans").set_defaults(fn=cmd_orphans)
    sub.add_parser("recover").set_defaults(fn=cmd_recover)
    a = sub.add_parser("doctor"); a.add_argument("--restore", action="store_true"); a.set_defaults(fn=cmd_doctor)
    a = sub.add_parser("log"); a.add_argument("--task"); a.add_argument("--epic"); a.add_argument("--action")
    a.add_argument("--today", action="store_true"); a.add_argument("-n", type=int, default=200)
    a.set_defaults(fn=cmd_log)

    jr = sub.add_parser("jira").add_subparsers(dest="cmd", required=True)
    a = jr.add_parser("epics"); a.add_argument("project"); a.add_argument("--search")
    a.set_defaults(fn=cmd_jira_epics)
    a = jr.add_parser("create-epic"); a.add_argument("project"); a.add_argument("summary")
    a.set_defaults(fn=cmd_jira_create_epic)
    a = jr.add_parser("search"); a.add_argument("project"); a.add_argument("query", nargs="?"); a.add_argument("--jql"); a.set_defaults(fn=cmd_jira_search)
    a = jr.add_parser("get"); a.add_argument("project"); a.add_argument("key"); a.set_defaults(fn=cmd_jira_get)
    a = jr.add_parser("comment"); a.add_argument("project"); a.add_argument("key"); a.add_argument("text"); a.set_defaults(fn=cmd_jira_comment)
    a = jr.add_parser("done"); a.add_argument("project"); a.add_argument("key"); a.set_defaults(fn=cmd_jira_done)
    a = jr.add_parser("transitions"); a.add_argument("project"); a.add_argument("key"); a.set_defaults(fn=cmd_jira_transitions)
    a = jr.add_parser("move"); a.add_argument("project"); a.add_argument("key"); a.add_argument("status"); a.set_defaults(fn=cmd_jira_move)
    a = jr.add_parser("rollup"); a.add_argument("project"); a.add_argument("--key"); a.add_argument("--to"); a.add_argument("--apply", action="store_true"); a.set_defaults(fn=cmd_jira_rollup)
    a = jr.add_parser("attachments"); a.add_argument("project"); a.add_argument("key"); a.set_defaults(fn=cmd_jira_attachments)
    a = jr.add_parser("pull"); a.add_argument("project"); a.add_argument("key"); a.add_argument("--out"); a.add_argument("--images-only", action="store_true"); a.set_defaults(fn=cmd_jira_pull)

    rp_ = sub.add_parser("repo").add_subparsers(dest="cmd", required=True)
    a = rp_.add_parser("add"); a.add_argument("project"); a.add_argument("name")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true"); g.add_argument("--clone"); g.add_argument("--path")
    a.add_argument("--remote"); a.add_argument("--run"); a.set_defaults(fn=cmd_repo_add)
    a = rp_.add_parser("set"); a.add_argument("project"); a.add_argument("repo")
    a.add_argument("--setup"); a.add_argument("--run"); a.add_argument("--reviewer"); a.add_argument("--remote")
    a.add_argument("--default-branch", dest="default_branch"); a.set_defaults(fn=cmd_repo_set)
    a = rp_.add_parser("ls"); a.add_argument("project"); a.set_defaults(fn=cmd_repo_ls)
    a = rp_.add_parser("members"); a.add_argument("project"); a.add_argument("repo"); a.set_defaults(fn=cmd_repo_members)

    ep = sub.add_parser("epic").add_subparsers(dest="cmd", required=True)
    a = ep.add_parser("add"); a.add_argument("project"); a.add_argument("key")
    a.add_argument("--summary"); a.add_argument("--target", action="append"); a.add_argument("--repos")
    a.set_defaults(fn=cmd_epic_add)
    a = ep.add_parser("ls"); a.add_argument("project", nargs="?"); a.set_defaults(fn=cmd_epic_ls)
    a = ep.add_parser("mr"); a.add_argument("key"); a.add_argument("--dry-run", action="store_true"); a.set_defaults(fn=cmd_epic_mr)
    a = ep.add_parser("mrs"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_mrs)
    a = ep.add_parser("plan"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_plan)
    a = ep.add_parser("merge"); a.add_argument("key"); a.add_argument("--dry-run", action="store_true"); a.add_argument("--squash", action="store_true"); a.set_defaults(fn=cmd_epic_merge)
    a = ep.add_parser("set"); a.add_argument("key"); a.add_argument("--summary"); a.add_argument("--repos"); a.add_argument("--target", action="append"); a.set_defaults(fn=cmd_epic_set)
    a = ep.add_parser("note"); a.add_argument("key"); a.add_argument("text"); a.set_defaults(fn=cmd_epic_note)
    a = ep.add_parser("memory"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_memory)
    a = ep.add_parser("sync"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_sync)
    a = ep.add_parser("open"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_open)
    a = ep.add_parser("ops"); a.add_argument("key"); a.add_argument("--kind", default="test")
    a.add_argument("--manual", action="store_true"); a.set_defaults(fn=cmd_epic_ops)
    a = ep.add_parser("done"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_done)
    a = ep.add_parser("archive"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_archive)
    a = ep.add_parser("unarchive"); a.add_argument("key"); a.set_defaults(fn=cmd_epic_unarchive)
    a = ep.add_parser("rm"); a.add_argument("key"); a.add_argument("--force", action="store_true"); a.set_defaults(fn=cmd_epic_rm)

    tk = sub.add_parser("task").add_subparsers(dest="cmd", required=True)
    a = tk.add_parser("add"); a.add_argument("epic"); a.add_argument("title", nargs="?", default="")
    a.add_argument("--prompt", required=True); a.add_argument("--repos")
    a.add_argument("--sync", action="store_true"); a.add_argument("--no-setup", action="store_true")
    a.add_argument("--jira", help="link to an EXISTING Jira issue key instead of creating one")
    a.add_argument("--no-jira", action="store_true", help="do not create a Jira task")
    a.add_argument("--manual", action="store_true", help="board entry + worktree but NO background agent (you do the work)")
    a.set_defaults(fn=cmd_task_add)
    a = tk.add_parser("ls"); a.add_argument("epic", nargs="?"); a.set_defaults(fn=cmd_task_ls)
    a = tk.add_parser("setup"); a.add_argument("task"); a.set_defaults(fn=cmd_task_setup)  # regen CLAUDE.md (fresh rules)
    a = tk.add_parser("regroup"); a.add_argument("task"); a.add_argument("group"); a.set_defaults(fn=cmd_task_regroup)
    a = tk.add_parser("diff"); a.add_argument("task"); a.set_defaults(fn=cmd_task_diff)
    a = tk.add_parser("open"); a.add_argument("task"); a.set_defaults(fn=cmd_task_open)
    a = tk.add_parser("done"); a.add_argument("task"); a.add_argument("--force", action="store_true"); a.set_defaults(fn=cmd_task_done)
    a = tk.add_parser("mr"); a.add_argument("task"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--no-ai", action="store_true", help="static commit/MR text (skip claude generation)"); a.set_defaults(fn=cmd_task_mr)
    a = tk.add_parser("mrs"); a.add_argument("task"); a.set_defaults(fn=cmd_task_mrs)
    a = tk.add_parser("merge"); a.add_argument("task"); a.add_argument("--dry-run", action="store_true"); a.add_argument("--squash", action="store_true"); a.set_defaults(fn=cmd_task_merge)
    a = tk.add_parser("abort"); a.add_argument("task"); a.set_defaults(fn=cmd_task_abort)
    return p

# commands that only read state -> no lock; everything else mutates -> serialize
def _scan_orphans(s):
    """Task work present on disk (cctui/<epic>/<slug>/<repo> worktrees, branch <epic>-<slug>) but
    with NO matching task in cc state — i.e. tasks that were silently dropped from the board.
    Returns [{project, epic, slug, branch, repos:{repo:wt_path}}]."""
    # match by worktree PATH, not by reconstructing "<epic>-<slug>": loose tasks have a plain-slug
    # branch (not <epic>-<slug>), so a branch guess would falsely flag on-board loose tasks as orphans.
    known_wts = set()
    for t in s["tasks"].values():
        for wt in (t.get("worktrees") or {}).values():
            try:
                known_wts.add(os.path.realpath(wt))
            except Exception:
                pass
    orphans = []
    for pname, proj in s.get("projects", {}).items():
        base = proj.get("path")
        if not base:
            continue
        cctui = Path(base) / "cctui"
        if not cctui.is_dir():
            continue
        for epic_dir in sorted(cctui.iterdir()):
            if not epic_dir.is_dir():
                continue
            epic = epic_dir.name
            for task_dir in sorted(epic_dir.iterdir()):
                if not task_dir.is_dir():
                    continue
                slug = task_dir.name
                repos = {}
                for repo_dir in sorted(task_dir.iterdir()):
                    if (repo_dir.is_dir() and (repo_dir / ".git").exists()
                            and repo_dir.name in proj.get("repos", {})):
                        repos[repo_dir.name] = str(repo_dir)
                if not repos:
                    continue
                # claimed by a known task if ANY of its worktrees is a known worktree path
                if any(os.path.realpath(p) in known_wts for p in repos.values()):
                    continue
                branch = slug if epic.endswith("__loose") else "%s-%s" % (epic, slug)
                orphans.append({"project": pname, "epic": epic, "slug": slug,
                                "branch": branch, "repos": repos})
    return orphans

def _state_issues(s):
    """Structural problems in the state (dangling refs / missing repos / missing paths). [(severity,msg)]."""
    issues = []
    projects, epics, tasks = s.get("projects", {}), s.get("epics", {}), s.get("tasks", {})
    for ek, e in epics.items():
        if e.get("project") not in projects:
            issues.append(("ERR", "эпик %s ссылается на несуществующий проект '%s'" % (ek, e.get("project"))))
    for tid, t in tasks.items():
        ep = t.get("epic")
        if ep not in epics:
            issues.append(("ERR", "задача %s ссылается на несуществующий эпик '%s'" % (tid, ep)))
            continue
        proj = projects.get(epics[ep].get("project"), {})
        for r in t.get("repos", []):
            if r not in proj.get("repos", {}):
                issues.append(("WARN", "задача %s: репо '%s' нет в проекте" % (tid, r)))
        for r, wt in (t.get("worktrees") or {}).items():
            if not os.path.isdir(wt):
                issues.append(("WARN", "задача %s: worktree пропал на диске (%s) — %s" % (tid, r, wt)))
    for pn, p in projects.items():
        if p.get("path") and not os.path.isdir(p["path"]):
            issues.append(("WARN", "проект %s: путь не существует (%s)" % (pn, p["path"])))
    return issues

def cmd_doctor(args):
    """Health check: state file integrity, dangling refs, missing worktrees, orphans, backups.
    --restore: replace a corrupt state.json from the newest valid backup."""
    print("== cc doctor ==")
    # 1. state file
    raw_ok = True
    if not STATE_FILE.exists():
        print("state.json: ОТСУТСТВУЕТ (пустой стейт)")
    else:
        try:
            d = json.loads(STATE_FILE.read_text())
            raw_ok = _valid_state(d)
            print("state.json: %s" % ("✓ валиден" if raw_ok else "✗ нет projects/epics/tasks"))
        except Exception as ex:
            raw_ok = False
            print("state.json: ✗ ПОВРЕЖДЁН (%s)" % str(ex)[:80])
    bks = sorted(_BACKUP_DIR.glob("state-*.json"), reverse=True) if _BACKUP_DIR.exists() else []
    good = _newest_valid_backup()
    print("бэкапов: %d (свежий валидный: %s)" % (len(bks), good.name if good else "НЕТ"))
    if not raw_ok:
        if getattr(args, "restore", False) and _newest_valid_backup():
            bk = _newest_valid_backup(); shutil.copy2(str(bk), STATE_FILE)
            audit("state.restore", detail=bk.name, reason="doctor")
            print("→ восстановлено из %s ✓" % bk.name)
        else:
            print("→ запусти `cc doctor --restore` чтобы восстановить из свежего бэкапа" if _newest_valid_backup()
                  else "→ валидного бэкапа НЕТ — разбери ~/.cc/backups/ вручную")
            return
    # 2. structural integrity + orphans (load is now self-healing)
    s = load_state()
    issues = _state_issues(s)
    errs = [m for sev, m in issues if sev == "ERR"]; warns = [m for sev, m in issues if sev == "WARN"]
    print("\nпроекты/эпики/задачи: %d/%d/%d" % (len(s["projects"]), len(s["epics"]), len(s["tasks"])))
    for m in errs:  print("  ✗ " + m)
    for m in warns: print("  ⚠ " + m)
    orph = _scan_orphans(s)
    if orph:
        print("  ⚠ потеряшек на диске: %d (`cc recover` вернёт)" % len(orph))
    if not errs and not warns and not orph:
        print("  ✓ всё консистентно — висячих ссылок и потеряшек нет")
    print("\nитог: %d ошибк(а), %d предупрежд., %d потеряшек" % (len(errs), len(warns), len(orph)))

def cmd_orphans(args):
    orphans = _scan_orphans(load_state())
    if not orphans:
        print("потеряшек нет — всё, что на диске, есть на доске ✓")
        return
    print("⚠️ %d задач(и) на диске, которых НЕТ на доске cc:" % len(orphans))
    for o in orphans:
        print("  epic %s / %s   (ветка %s; репо: %s)" % (o["epic"], o["slug"], o["branch"], ", ".join(o["repos"])))
    print("\n`cc recover` вернёт их на доску (worktrees/ветки уже на диске — работа цела).")

def cmd_recover(args):
    def fn(s):
        recovered = []
        for o in _scan_orphans(s):
            epic, pname = o["epic"], o["project"]
            proj = s["projects"][pname]
            if epic.endswith("__loose"):
                ensure_loose_epic(s, pname)   # epic-less task -> the project's loose container
            elif epic not in s["epics"]:
                s["epics"][epic] = {"project": pname, "summary": "(восстановлен)",
                                    "mode": "epic_branch", "branch": epic, "targets": {}, "mrs": {}}
            tid = _unique_tid(s, epic, o["slug"])
            base = {r: target_for(s["epics"][epic], proj, r) for r in o["repos"]}   # honors epic/loose targets
            s["tasks"][tid] = {
                "epic": epic, "title": o["slug"], "branch": o["branch"],
                "repos": list(o["repos"].keys()), "worktrees": dict(o["repos"]),
                "base": base, "mrs": {}, "log": str(STATE_DIR / (tid + ".log")),
                "recovered": True,
            }
            recovered.append((tid, epic, o["branch"]))
        return recovered
    recovered = mutate(fn)
    if not recovered:
        print("нечего восстанавливать — потеряшек на диске нет ✓")
        return
    audit("state.recover", count=len(recovered), detail=",".join(t for t, _, _ in recovered))
    print("восстановлено %d задач(и) на доску:" % len(recovered))
    for tid, epic, br in recovered:
        print("  %s   (epic %s, ветка %s)" % (tid, epic, br))
    print("\n`cc task mrs <tid>` подтянет MR; имя = slug (исходный заголовок не сохранялся в worktree).")

_LOG_FIELDS = ("repo", "base", "mr", "url", "to", "key", "branch", "default_branch",
               "remote", "title", "repos", "skipped", "count", "reason", "detail")

def cmd_log(args):
    """`cc log` — the action timeline (newest first): what cc DID, when, to which task/epic/repo.
    Filter with --task/--epic/--action/--today; limit with -n. Reads ~/.cc/audit.log (append-only)."""
    since = None
    if getattr(args, "today", False):
        since = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
    recs = read_audit(task=getattr(args, "task", None), epic=getattr(args, "epic", None),
                      action=getattr(args, "action", None), since=since, limit=getattr(args, "n", 200))
    if not recs:
        print("(аудит пуст — записанных действий ещё нет)")
        return
    for r in recs:
        ts = time.strftime("%H:%M %d.%m", time.localtime(r.get("ts", 0)))
        who = r.get("task") or r.get("epic") or r.get("project") or ""
        extra = []
        for k in _LOG_FIELDS:
            if k in r and k != who:
                v = r[k]
                if isinstance(v, list):
                    v = ",".join(map(str, v))
                extra.append("%s=%s" % (k, v))
        print("%s  %-15s %-20s %s" % (ts, r.get("action", "?"), who, "  ".join(extra)))

_READONLY = {cmd_task_diff, cmd_task_ls, cmd_repo_ls, cmd_repo_members,
             cmd_epic_ls, cmd_epic_memory, cmd_project_ls, cmd_jira_epics, cmd_orphans, cmd_epic_plan,
             cmd_jira_search, cmd_jira_get, cmd_jira_comment, cmd_jira_done,
             cmd_jira_attachments, cmd_jira_pull,
             cmd_jira_transitions, cmd_jira_move, cmd_jira_rollup, cmd_doctor, cmd_log,
             cmd_task_setup}  # no cc-state writes; network/file off the lock

_SELF_LOCKED = {cmd_epic_mrs, cmd_task_mrs, cmd_repo_add, cmd_project_new, cmd_recover,
                cmd_task_merge, cmd_epic_merge}   # do git/network lock-free, then save under a brief mutate() themselves

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
