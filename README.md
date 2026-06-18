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

cc project add ~/code/myproject                   # detect single/multi repo
cc epic add myproject FEAT-1 --summary "Feature"  # epic (its own branch -> master)
cc epic add myproject FEAT-1 --target web=develop --target api=release   # or route per repo
cc task add FEAT-1 "add X" --prompt "..."         # background agent edits the repos
cc task diff t_add-x                              # combined diff across repos
cc task open t_add-x                              # Cursor multi-root + run hints
cc task mrs t_add-x                               # MR links + state (open/merged)
cc task mr  t_add-x --dry-run                     # preview the MR commands
cc task mr  t_add-x                               # push + one MR per repo to the epic target
cc task done t_add-x                              # safety-checked worktree cleanup
```

### TUI keys
`a` +Project · `e` +Epic · `n` +Task · `o` chat (interactive, new terminal/cmux tab) ·
`v` view chat (read-only) · `c` Cursor · `d` diff · `m`/`M` MR dry/create ·
`g` MR links · `x` cleanup · `R` reviewers · `r` refresh · `q` quit.

### Reviewers / assignee
Assignee defaults to your `glab` user. Set a reviewer per repo from the GitLab
member list — no hunting usernames: TUI `R`, or `cc repo set <proj> <repo> --reviewer <user>`
(`cc repo members <proj> <repo>` lists them).

### Epic memory
Each epic carries notes (`cc epic note <KEY> "invariant/decision/gotcha"`,
`cc epic memory <KEY>` to view) that `cc` injects into every task agent of that
epic via a `CLAUDE.md` — so agents stay consistent and don't repeat mistakes.

## Jira (optional)
```bash
cc project jira myproject --site you.atlassian.net --email you@x.com --token <API_TOKEN> --project-key ABC
cc jira epics myproject                # your epics
```
With Jira on, the epic modal lists **your** epics to pick (with search) or
creates a new one in Jira; tasks you fire are created as Jira tasks under the
epic. The token is stored in `~/.cc/state.json` (chmod 600) and never logged.

## How it works
- **Isolation = git worktrees** (like Conductor) — shared `.git`, separate
  working dir + branch per repo. Cheap and fast; `cc` re-provides `.env`
  (copied) and `node_modules` (symlinked) per worktree.
- **Agent session** runs headless in the background (autonomous); `o` opens an
  interactive session (skills + pickers) to steer it.
- State lives in `~/.cc/state.json`; git/MRs are the source of truth (statuses
  are derived live: running / review / mr / merged).

MIT licensed. Not affiliated with Anthropic, GitLab, or Atlassian.
