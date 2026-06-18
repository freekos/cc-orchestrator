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
cc task add FEAT-1 "add X" --prompt "..." --jira ABC-12   # link to an EXISTING Jira issue (no new one)
cc task diff t_add-x                              # combined diff across repos
cc task open t_add-x                              # Cursor multi-root workspace + embedded "Run Task" to start every repo
cc task mrs t_add-x                               # MR links + state (open/merged)
cc task mr  t_add-x --dry-run                     # preview the MR commands
cc task mr  t_add-x                               # push + one MR per repo to the epic target
cc task done t_add-x                              # safety-checked worktree cleanup (refuses if dirty)
cc task abort t_add-x                             # remote teardown: close MRs + delete remote branches + local cleanup

cc epic mr  FEAT-1                                # MR the epic branch -> master/main (after its tasks merged in)

cc deploys myproject                              # what's live per repo: dev/stage/prod ref@sha (EAS for Expo repos)
cc epic archive FEAT-1                            # archive: hide under "Архив" + push epic & its tasks to Done in Jira
cc epic unarchive FEAT-1                          # bring it back to the live list
```

### TUI keys
`a` +Project · `e` +Epic · `n` +Task · `o` chat (interactive, new terminal/cmux tab) ·
`v` view chat (read-only) · `c` Cursor · `d` diff · `m`/`M` MR dry/create ·
`g` MR links · `D` refresh deploy status · `x` cleanup task / archive epic ·
`R` reviewers · `r` refresh · `q` quit.

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

### Reviewers / assignee
Assignee defaults to your `glab` user. Set a reviewer per repo from the GitLab
member list — no hunting usernames: TUI `R`, or `cc repo set <proj> <repo> --reviewer <user>`
(`cc repo members <proj> <repo>` lists them).

### Epic memory
Each epic carries notes (`cc epic note <KEY> "invariant/decision/gotcha"`,
`cc epic memory <KEY>` to view) that `cc` injects into every task agent of that
epic via a `CLAUDE.md` — so agents stay consistent and don't repeat mistakes.

### Epics & archive
Live epics sit at the top of each project; **archived** ones collapse under a
**"🗄 Архив (N)"** node at the bottom (collapsed by default, dimmed). Archive — TUI
`x` on an epic, or `cc epic archive <KEY>` — hides the epic locally **and** pushes it
plus all its tasks to Done in Jira. `cc epic unarchive <KEY>` brings it back (Jira
status is not reopened). Deleting epics is intentionally not in the UI; archive is the
lifecycle end-state.

## Jira (optional)
```bash
cc project jira myproject --site you.atlassian.net --email you@x.com --token <API_TOKEN> --project-key ABC
cc jira epics myproject                # list project epics (most-recent first)
```
With Jira on:
- **Epic modal** lists all the project's epics (most-recent first, with status) to
  pick from; the search box narrows by name across **all** project epics. Or create a
  brand-new epic in Jira from the same modal.
- **Task modal** (under a Jira epic) can pull the epic's **child issues from Jira**:
  pick one and *Подставить из Jira* seeds the title from its summary and the prompt
  from its description, and links the cc task to that issue — no duplicate. Or just
  type a title + prompt and `cc` creates a new Jira task under the epic.
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
- State lives in `~/.cc/state.json`; git/MRs are the source of truth (statuses
  are derived live: running / review / mr / merged).

MIT licensed. Not affiliated with Anthropic, GitLab, or Atlassian.
