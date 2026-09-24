# flow

A command-line tool for working on several tasks at once in one git repository. Each task gets its own branch,
worktree, and task file. `flow` reads the state of each task's merge request from GitLab or GitHub and prints one
table of all tasks. Installed as a Claude Code hook, it puts that table into the agent's context at the start of
every session.

```
$ flow status
  key     stage       ball branch                          wt      mr          updated next
  PROJ-72 review-wait me   PROJ-72-export-csv +14/-2 ⇡2    ok      #36:openedD 1d      Mark the PR ready for review
* PROJ-51 review-wait them PROJ-51-login-timeout +3/-4     primary #33:opened  8d      Wait for review on #33
  PROJ-74 changes     me   PROJ-74-rate-limits +13/-2      dirty   #34:opened  1d      Answer the threads on #34
  PROJ-42 merged      me   PROJ-42-ci-cache +6/-28         primary #4:merged   9d      Clean up
```

| Column | Meaning |
|---|---|
| `*` | The task of the branch you are on. |
| `key` | Task key, Jira-style (`ABC-123`). |
| `stage` | Where the task is; see [Concepts](#concepts). |
| `ball` | Who acts next: `me` (you) or `them` (reviewers, the author, anyone else). |
| `branch` | Branch name, then commits ahead/behind `main` (`+14/-2`). `⇡2`: 2 commits not pushed. `local`: never pushed. |
| `wt` | The task's worktree: `ok` (clean), `dirty` (uncommitted changes), `primary` (no own worktree), `missing`. |
| `mr` | Merge request (PR on GitHub): `#36:opened`. `D`: draft. `?`: the forge has not answered yet. |
| `updated` | Days since the task file last changed. |
| `next` | The next action, written by you or the agent. |

## Why

An AI agent starts every session without memory: it does not know which branch belongs to which task, what is
pushed, or whose move it is on each review. `flow` gives it all of that before its first message. It reads a local
cache, so the hook makes no network calls.

## Before you install

- **Forge:** GitLab or GitHub only, with its CLI authenticated: [`glab`](https://gitlab.com/gitlab-org/cli) or
  [`gh`](https://cli.github.com). Self-hosted GitLab and GitHub Enterprise work.
- **Task keys:** Jira-style (`ABC-123`). Branches are named `<KEY>-<slug>`.
- **`local-docs/`:** `flow` stores its files in `local-docs/` at the repository root and hides it from git. If your
  repository already tracks a `local-docs/`, rename it first.
- **Tools:** git and [uv](https://docs.astral.sh/uv/).

## Install

```bash
uv tool install git+https://github.com/danielPashht/flow-worktrees.git
flow install-hook      # optional: adds the Claude Code SessionStart hook to ~/.claude/settings.json
```

`flow install-hook --print` prints the hook's JSON instead of editing the file. Upgrade with
`uv tool upgrade flow-worktrees`.

## Quick start

Run these in your repository's main checkout.

```
$ flow start PROJ-12 login-timeout --title "Fix login timeout"
worktree ../myrepo-12
branch   PROJ-12-login-timeout
task     local-docs/tasks/PROJ-12.md

$ cd ../myrepo-12
$ flow status
  key     stage ball branch                            wt mr updated next
* PROJ-12 plan  me   PROJ-12-login-timeout +0/-0 local ok -  today   refine: read the task, fill notes and next
```

Work in the worktree. Record why you did something and what comes next:

```
$ flow note "timeout comes from the proxy, not the app" --next "raise proxy_read_timeout"
PROJ-12: noted; next = 'raise proxy_read_timeout'
```

Move through the stages with `flow next`. From `test`, it runs your check, requires a push, and opens a draft MR:

```
$ flow next
PROJ-12: plan → refine
$ flow next
PROJ-12: refine → implement
$ flow next
PROJ-12: implement → test
$ git commit -am "PROJ-12: raise proxy timeout" && git push -u origin HEAD
$ flow next
gate: true
created Draft PR #1: PROJ-12: Fix login timeout
PROJ-12: test → review-wait (ball=me, from #1)
```

From here, stage and ball follow the MR. `flow log` shows what happened:

```
$ flow log
2026-09-24        note        timeout comes from the proxy, not the app
2026-09-24 14:29  branch      bf5608dc branch: Created from origin/main
2026-09-24 14:29  commit      ab964f97 commit: PROJ-12: raise proxy timeout
2026-09-24 14:29  push        ab964f97 update by push
2026-09-24 14:29  mr-created  #1
```

After the merge, `flow clean PROJ-12` removes the worktree and the branch and archives the task file.

## Concepts

**Task file.** `local-docs/tasks/<KEY>.md`: YAML fields plus free notes. It stores only what the forge cannot know:
title, next action, blockers, branch, worktree, and the stage before review. Everything else is computed.

**Stage.** The first four are stored in the task file and advanced by `flow next`. The rest are computed from the MR.

```
plan → refine → implement → test → review-wait ⇄ changes → merge-wait → merged → cleaned
                                                                   ↘ parked (with a reason)
```

| Stage | Meaning |
|---|---|
| `review-wait` | The MR waits for someone: for you to mark the draft ready, or for reviewers. |
| `changes` | Someone left a comment you have not answered, or the MR is approved but cannot merge yet. |
| `merge-wait` | Approved, mergeable, no open threads. |
| `merged` | Merged; waits for `flow clean`. |
| `parked` | Blocked on something outside the task; `blocked_on` says what. |

**Ball.** Who acts next. On an open MR: whoever did not write last in an unresolved review thread owes a reply. If
the computed ball is wrong (for example, you wait for an answer in chat), `flow set ball them --why "…"` pins it
until the MR changes.

**Gate.** The check `flow next` runs before review: any shell command, set as `gate:` in the config. Without it,
`flow next --no-gate --why "…"` skips the check and records the reason in the task file.

**Cache.** Answers from the forge live in `local-docs/.flow-cache/forge.json`. Online commands refresh it. When it
is older than an hour, the hook starts one refresh in the background.

**Human-only commands.** `start`, `clean`, `migrate`, `set stage parked`, and `install-hook` refuse to run inside
Claude Code (`CLAUDECODE=1`). The agent cannot start, park, or remove your tasks.

## Everyday commands

| Command | Does |
|---|---|
| `flow status` | Table of all tasks. `--json`, `--md`, `--tsv` for other formats. |
| `flow board` | The same tasks as a kanban board in the browser. |
| `flow next [KEY]` | Advances the stage; from `test`, sends the task to review. |
| `flow note "…" [KEY] --next "…"` | Adds a dated note and sets the next action. |
| `flow log [KEY]` | Timeline: commits, pushes, MR events, notes. |
| `flow doctor [--fix]` | Finds broken symlinks, branches without tasks, stale merged tasks. |
| `flow clean KEY` | After the merge: removes worktree and branch, archives the task. |
| `flow guide` | The full guide: every rule for stage and ball, safeguards, migration. |

## Configuration

Optional. File `local-docs/flow.local.yml`:

```yaml
forge: github                 # gitlab | github; default: detected from origin's host
gate: make check              # the check before review
base_branch: main             # default: origin/HEAD
worktree_dir: ../myrepo-{n}   # default: ../<repo dir minus its last -part>-<task number>
jira_base: https://jira.example.com
overlap_ignore: [".metrics/*"]
approvals_required: 1
```

## Limits

- A PR from a fork is not found: `flow` looks up branches in the repository itself.
- On GitHub, an unresolved thread blocks the merge only if branch protection requires it. `flow` passes the ball on
  it either way.
- A plain comment outside a review thread does not pass the ball.
- `flow` records a task's git history when you run `sync`, `log`, or `clean`. Git keeps that history 90 days.

## Development

```bash
uv run pytest     # each test uses a temporary git repo and fake glab/gh; no network
```

## License

MIT.
