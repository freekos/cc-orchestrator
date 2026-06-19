# cc — multi-repo, epic-routed AI-agent orchestrator

Run many AI coding tasks in parallel and review them comfortably. `cc` drives
**Claude Code** locally, gives each task its own git **worktree(s)**, lets a
**single task span several repos at once**, groups tasks under a **(Jira) epic**
that decides each repo's MR target branch, and shows it all in a terminal UI.

**Core invariant:** the agent only EDITS files — `cc` owns all git
(branches, commits, MRs). Nothing is auto-merged or auto-deleted.

## Why
The single Claude Code terminal makes you wait, and one task = one repo. Real
features touch several repos. `cc` gives you: parallel tasks, a chat per task,
worktree isolation, one task across many repos, epic-grouped review-first, and
GitLab/GitHub MRs whose target branch is decided at the epic level — which no
off-the-shelf tool does.

## Model
```
Project ▸ Repos
Project ▸ Epic ▸ Task ▸ RepoWork
```
- **Epic** decides, per repo, the integration branch its tasks merge into
  (the MR router). With no explicit target, the epic gets its own branch
  (`<EPIC>`), tasks merge into it, and that branch MRs to master/main.
- **Task** = one prompt that may touch several repos. Each repo gets a worktree
  under `<project>/cctui/<epic>/<task>/<repo>`; one Claude agent edits them all.

## Install
```bash
python3 -m venv ~/.cc/venv && ~/.cc/venv/bin/pip install textual   # TUI
brew install tmux            # optional
alias cc="/path/to/cc-orchestrator/cc"   # add to your shell rc
```
Requires: `git`, the `claude` CLI (Claude Code), and `glab`/`gh` for MRs.

## Use
```bash
cc tui                                            # the terminal UI (recommended)

cc project add ~/code/myproject                   # detect single/multi repo (empty folder -> empty project)
cc project new myapp                              # create a BRAND-NEW empty project (folder + entry)
cc repo add myapp backend --new --run "uv run dev"   # add a repo: --new (git init) | --clone <url> | --path <dir>
cc repo set myapp backend --remote group/backend  # set the GitLab/GitHub remote later
cc project setup myapp                            # open the AI "setup architect" chat (interview -> repos + AI config)
cc epic add myproject FEAT-1 --summary "Feature"  # epic (its own branch -> master)
cc epic add myproject FEAT-1 --target web=develop --target api=release   # or route per repo
cc task add FEAT-1 "add X" --prompt "..."         # background agent edits the repos
cc task add FEAT-1 "add X" --prompt "..." --jira ABC-12   # link to an EXISTING Jira issue (no new one)
cc task diff t_add-x                              # combined diff across repos
cc task open t_add-x                              # Cursor multi-root workspace + embedded "Run Task" to start every repo
cc task mrs t_add-x                               # MR links + state (open/merged)
cc task mr  t_add-x --dry-run                     # preview the MR commands
cc task mr  t_add-x                               # push + one MR per repo to the epic target
cc task done t_add-x                              # safety-checked worktree cleanup (refuses if dirty)
cc task abort t_add-x                             # remote teardown: close MRs + delete remote branches + local cleanup

cc epic mr  FEAT-1                                # MR epic branch -> master/main — only in repos where it's ahead (tasks merged in)
cc epic mrs FEAT-1                                # show this epic's MR(s) to master/main (any state, incl. merged)

cc deploys myproject                              # what's live per repo: dev/stage/prod ref@sha (EAS for Expo repos)
cc task done t_add-x                              # "done & remove from board": Jira->Done + snap worktrees + drop from cc
cc epic done FEAT-1                               # finish epic: Jira(epic+tasks)->Done + remove epic & its tasks from cc
```

### TUI keys
`a` +Project · `e` +Epic · `n` +Task · `o` chat (interactive, new terminal/cmux tab) ·
`o` chat / project setup · `v` view chat (read-only) · `c` Cursor · `d` diff · `m`/`M` MR dry/create ·
`g` MR links (task & epic→master) · `D` refresh deploy status · `x` finish (task/epic → done & remove) ·
`R` reviewers · `r` refresh · `U` recover lost tasks · `q` quit.

### Deploy status
The project panel shows, per repo, what's currently deployed — `stage:<ref> prod:<ref>`
pulled from the GitLab Environments/Deployments API (the real "what's live", not just the
latest pipeline), or the latest EAS staging update for Expo/mobile repos. `D` refreshes.

### Running the repos in Cursor
`c` (or `cc task open <task>`) writes a multi-root `.code-workspace` and opens it in
Cursor — every repo of the task is a folder. It also **embeds VS Code/Cursor tasks**, so
to start all the dev servers at once: **Cmd+Shift+P → "Tasks: Run Task" → "cc: dev all"**
— each repo launches in its own dedicated terminal panel (using the repo's detected `run`
command). Prefer one terminal? `cc task open` also prints a ready-to-paste
`npx concurrently …` one-liner. Set/fix a repo's command with
`cc repo set <proj> <repo> --run "npm run dev"`.

### New project (greenfield, AI-ready)
Start a project from nothing: `cc project new <name>` (or `cc project add` on an empty folder)
makes an empty project, then **`o` on the project node** opens an **AI "setup architect" chat**
(`cc project setup`). It interviews you — what the project is, the stack, **which repos you want
(frontend/backend kept separate)**, and any code references — then creates each repo
(`cc repo add … --new`), scaffolds it, and writes the project's **AI config**: a project + per-repo
`CLAUDE.md`, plus `.claude/` skills, MCP servers and rules. Remotes are optional and set later
(`cc repo set <p> <repo> --remote <slug>`); until then commits are local and MRs are skipped.
After setup it's a normal cc project — epics, tasks, worktrees, MRs — and the `CLAUDE.md`
grounds every task agent.

### Reviewers / assignee
Assignee defaults to your `glab` user. Set a reviewer per repo from the GitLab
member list — no hunting usernames: TUI `R`, or `cc repo set <proj> <repo> --reviewer <user>`
(`cc repo members <proj> <repo>` lists them).

### Epic memory
Each epic carries notes (`cc epic note <KEY> "invariant/decision/gotcha"`,
`cc epic memory <KEY>` to view) that `cc` injects into every task agent of that
epic via a `CLAUDE.md` — so agents stay consistent and don't repeat mistakes.

### Task list — flat, sorted by what needs you
Under each epic the tasks are a **flat list, sorted by attention**: 🟡 review (agent
finished / has new local edits — needs you) first, then 🔵 running, 🟣 MR open, and ✅ done
(dimmed, at the bottom). The status emoji is the signal — no folders to expand. A **💬**
badge marks tasks whose agent wrote output you haven't opened yet (cleared on `o`/`v`).
A merged task that you **edit again flips back to 🟡** (new uncommitted changes, or commits
beyond the merge point — squash-merge proof via a recorded merge sha), so "done" never hides
fresh work. Add a Jira issue as a task via search in the create-task modal (`n`); there's no
separate Jira-stub list cluttering the tree.

### Epics — MR view & finishing
Selecting an epic shows its **MR(s) to master/main** in the detail pane (clickable, with
state — opened/merged), looked up lazily from GitLab the first time you focus it and on `g`.
In **targets** mode this is each integration branch's MR to master (or a `<KEY>/…` release
branch); in **epic-branch** mode it's the epic branch's MR. `M` on an epic creates that MR in
epic-branch mode, or just finds the existing release MRs in targets mode (those branches are
team-owned and released by their own flow).

**Finishing — "done & remove from board".** The cc board is your active working set; finished
work leaves it (the record lives in Jira + git, not in cc). Press `x` on a **task** →
*Готово и убрать с доски* (`cc task done`: its Jira issue → Done, snap worktrees, drop from
cc; refuses on uncommitted changes unless forced; remote MR/branch left intact; *Abort* is
the throw-away path). Press `x` on an **epic** → *Готово и убрать с доски* (`cc epic done`:
the epic **and all its tasks** → Done in Jira, then remove them from cc locally; remote
untouched). There is no archive/hide state and no delete-but-keep — finishing == removing.

**Knowledge survives.** Finishing an epic first stashes its accumulated notes/decisions
(`cc epic note` memory + mode/targets/done-date) into a local `epic_knowledge` store **and**
mirrors them as a comment on the Jira epic. Re-add the same epic later (`cc epic add <KEY>`)
for follow-up work and cc **restores its notes automatically** ("↻ восстановлены знания
закрытого эпика") — the board stays an active set, the feature's wisdom isn't lost.

## Jira (optional)
```bash
cc project jira myproject --site you.atlassian.net --email you@x.com --token <API_TOKEN> --project-key ABC
cc jira epics myproject                # list project epics (most-recent first)
```
With Jira on:
- **Epic modal** lists all the project's epics (most-recent first, with status) to
  pick from; the search box narrows by name across **all** project epics. Or create a
  brand-new epic in Jira from the same modal.
- **Add a Jira child as a task**: press `n` under a Jira epic and the create-task modal
  lists the epic's child issues — pick one and *Подставить из Jira* seeds the title+prompt
  and links it (`--jira`); Launch creates the worktree+agent. Jira children are **not**
  rendered in the tree as stubs — you pull them on demand when creating a task. `r` on an
  epic still re-syncs the child list from Jira for the modal.
- **Task modal** (under a Jira epic) can also pull the epic's children directly: pick
  one and *Подставить из Jira* seeds title+prompt and links it. Or just type a
  title + prompt and `cc` creates a new Jira task under the epic.
- **Archiving an epic** moves the epic issue **and all its child tasks** to Done in
  Jira (matched by status category, so it works with "Готово"/"Done"; already-done
  issues are skipped, each transition is reported).

The token is stored in `~/.cc/state.json` (chmod 600) and is never logged or written
to any repo/MR.

## How it works
- **Isolation = git worktrees** (like Conductor) — shared `.git`, separate
  working dir + branch per repo. Cheap and fast; `cc` re-provides `.env`
  (copied) and `node_modules` (symlinked) per worktree.
- **Agent session** runs headless in the background (autonomous); `o` opens an
  interactive session (skills + pickers) to steer it.
- State lives in `~/.cc/state.json`, written **atomically** (tmp + `os.replace`) under a
  cross-process **file lock** so concurrent `cc` commands + the TUI never lose updates
  (reads stay lock-free). Every write that changes the **set** of epics/tasks first
  snapshots the previous state to `~/.cc/backups/` (last 80), so a dropped item is always
  recoverable. The agent's work lives in git worktrees regardless: `cc orphans` lists task
  worktrees on disk that aren't on the board, and `cc recover` (TUI: **U**) re-imports them —
  the TUI also shows a **"⚠️ Потеряшки на диске"** node + a startup warning so a lost task
  can never go unnoticed. git/MRs are the source of truth (statuses are derived live:
  running / review / mr / merged).
- **Lightweight status polling.** The TUI never shells `git` on a loop in steady state: a
  cheap pid check (8s) tracks running agents and detects the moment one finishes; the
  expensive git probe runs **once** per task on that running→finished edge (or on manual
  `r`), then the verdict is cached until you act on it. Likewise the detail pane's
  per-repo **working-tree changes** are fetched **once** (lazily, off-thread) the first
  time you rest on a task and then cached — moving the cursor around the tree never
  re-shells `git`; press `r` on a task to re-fetch its changes. Background pollers run on
  daemon threads and every `git`/`glab` call is timeout-bounded, so quitting `cc tui` is
  instant and it doesn't thrash your CPU.

MIT licensed. Not affiliated with Anthropic, GitLab, or Atlassian.
