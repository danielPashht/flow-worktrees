# flow

**AI agents start every session from zero and mix tasks up. `flow` hands the agent the state of every task before
its first message.**

Branch, worktree, pushed or not, MR, whose move it is, and the next action: the agent reads all of it in the session
context instead of digging through git to find where it left off, and without a single network call, because the
hook reads a local cache. The same table saves the human the morning archaeology when ten tickets are open at once.

The task file holds only what git and GitLab don't know: the next action, blockers, "why" notes, and the local
stage of work. The MR, the stage after it, and whose move it is are computed from GitLab, and commits, pushes, and
review events are journalled automatically. A field nobody updated can't be wrong, because it isn't stored.

```
  key     stage       ball branch                                  wt      mr          updated next
  PROJ-72 review-wait me   PROJ-72-execution-run-prepare +14/-2 ⇡2 ok      !36:openedD 1d      Local merge not pushed…
* PROJ-51 review-wait them PROJ-51-us-ledger            +3/-4      primary !33:opened  8d      Wait for review on !33
  PROJ-74 changes     me   PROJ-74-jira-property-…      +13/-2     dirty   !34:opened  1d      Answer the threads on !34
  PROJ-42 merged      me   PROJ-42-ci-jsonl             +6/-28     primary !4:merged   9d      Ready to clean
for the human (own terminal): flow clean PROJ-42; un-draft !36 (PROJ-72) in GitLab
overlaps:
  PROJ-51 <- main moved under it: 7ffab14 PROJ-20: Implement reproducible releases...; shared files: …
  PROJ-51 <-> PROJ-74: both change Taskfile.yml
```

## Install

Requires [uv](https://docs.astral.sh/uv/), git, and [`glab`](https://gitlab.com/gitlab-org/cli) authenticated for
your GitLab.

```bash
uv tool install flow-worktrees          # from PyPI
# or straight from the repository:
uv tool install git+https://github.com/danielPashht/flow-worktrees.git
```

That puts `flow` on your `PATH`. To have Claude Code show the task summary at the start of every session, run once:

```bash
flow install-hook                       # edits ~/.claude/settings.json, keeps a .bak
flow install-hook --print               # or print the JSON snippet and merge it yourself
```

Upgrade with `uv tool upgrade flow-worktrees`; remove with `uv tool uninstall flow-worktrees`.

## What it gives you

- **One task: one branch, one worktree, one file.** `flow start PROJ-4801 slug` creates a branch from `main`, its
  own worktree, and the task file. Tasks don't interfere, and each can run in its own session.
- **State from the first minute.** At session start the agent gets the table: stage, whose move it is, how far the
  branch has drifted from `main` and whether it is pushed, the MR, and what to do next. The hook stays offline: it
  reads the GitLab cache and, if the cache is older than an hour, refreshes it in the background for next time.
- **MR, stage, and ball come from GitLab.** The MR is found by branch; the stage after it and the ball follow from
  its state and threads, whether you are the author or a reviewer. When the computed answer is wrong (you're waiting
  on a chat reply), pin it with a reason, `flow set ball them --why "…"`, until the MR next moves.
- **A journal without discipline.** `flow log` shows the task's timeline: commits, amends, rebases, and pushes from
  the reflog; MR creation, review requests, approvals, and comments from GitLab; and your notes in between. Only
  the "why" is left to write by hand.
- **Overlaps before they conflict.** Under the table, `overlaps:` lists tasks that `main` moved under (with the
  commit and the shared files), pairs of branches changing the same files, and stacked branches. Git only, offline.
- **Checked transitions.** `flow next` advances the pre-MR stages. A task goes to review only with a green gate, a
  branch that really exists on origin, and a clean worktree with no unfinished merge or rebase; the Draft MR is
  created for you.
- **Separated roles.** The human's decisions — start, un-draft, merge, clean up — cannot be taken by the agent:
  those commands refuse inside a Claude Code session. The agent drives the rest.
- **Self-diagnosis.** `flow doctor` finds broken symlinks, branches without tasks, tasks without branches,
  misplaced worktrees, closed MRs, stale pins, and "merged a week ago but never cleaned".
- **Doesn't lose work.** `flow clean` won't delete a branch holding commits that are on no remote and not in the
  merged MR, even with `--force-unmerged`.
- **A board.** `flow board` opens a kanban in the browser: work / review / done / parked, with stale items in red.

## A day with flow

```bash
flow start PROJ-4801 short-slug --title "What we are doing"  # human: branch + worktree + task file
cd ../myrepo-4801 && claude                                  # an agent session in its own worktree

flow note "found the cause in X" --next "fix X"   # agent: the why, and the next action
flow next                                          # plan → refine → implement → test; from test: review + Draft MR
flow status                                        # where every task is (online: also refreshes the GitLab cache)
flow log                                           # what happened to the current task
flow clean PROJ-4801                               # human: after the merge, remove the worktree and branch
```

```
plan → refine → implement → test → review-wait ⇄ changes → merge-wait → merged → cleaned
└──── stored in the file ───────┘  └────────── computed from the MR ────────┘
                                                                   ↘ parked (with a reason)
```

The full workflow, the rules that derive stage and ball, and the safeguards are in the guide: `flow guide`
([source](src/flow_worktrees/guide.md)).

## Configuration

Optional, per repository, in `local-docs/flow.local.yml`: `gate` (the pre-review check; without it `flow next`
from `test` requires `--no-gate --why "…"`), `base_branch`, `worktree_dir`, `jira_base`, `overlap_ignore`,
`approvals_required`. `flow --help` describes each.

## Limits

- The forge is GitLab only, through `glab`; a GitHub backend would be a second class beside `Glab`.
- Task keys are Jira-style (`ABC-123`); this is not configurable.
- The `local-docs/` directory name is fixed. If your repository already tracks a `local-docs/`, rename it first.
- `local-docs/` is hidden through `.git/info/exclude` on the first `flow start`. A task file created by hand leaves
  the directory visible in `git status` until then.
- Without GitLab access, transitions that need it refuse and say why; `flow status` shows cached stages and the
  cache's age.
- The ball follows resolvable threads. A plain comment outside a thread does not pass it; a bot's thread does,
  because GitLab blocks the merge on it.
- The SessionStart hook may spawn a detached `flow sync`. `FLOW_BACKGROUND_SYNC=0` disables that.

## Development

```bash
uv run pytest        # a throwaway git repo per test, with a fake glab: no network, no auth
```

## License

MIT.
