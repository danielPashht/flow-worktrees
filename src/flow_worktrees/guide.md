# flow — user guide

One worktree = one task = one Claude session. A task's state is one file, `local-docs/tasks/<KEY>.md` (YAML
frontmatter + notes), shared by the primary checkout and every worktree through a symlink. None of it is committed.
`flow --help` is the command reference; this guide is the working order.

## Forges

flow reads GitLab through `glab` and GitHub through `gh`, and says "MR" for both; on GitHub read it as "PR", and
numbers print as `#N` instead of `!N`. The forge comes from `forge:` in `local-docs/flow.local.yml` when set, else
from `origin`'s host: a host containing `github` is GitHub (Enterprise included), one containing `gitlab` is GitLab.
Any other host needs `forge:`; until it is set, offline commands and the hook keep working and only calls to the
forge refuse.

On GitHub the rules below read these facts:

- **approvals** — reviewers whose latest review is APPROVED;
- **threads** — review threads, resolved or not;
- **changes requested** — each standing CHANGES_REQUESTED verdict is one more open thread. Its last word is the
  reviewer's until the author pushes after it, then the author's: the change is owed first, the re-review after;
- **mergeable** — `mergeStateStatus` is CLEAN.

## What is stored and what is computed

The task file holds only what the forge cannot know: `next`, `blocked_on`, `title`, `docs`, notes, the branch, the
worktree, and the local stage (`plan`/`refine`/`implement`/`test`, `parked`). The MR is found by branch; the post-MR
stages (`review-wait`/`changes`/`merge-wait`/`merged`) and the ball are derived from the MR and its threads.

The forge's answers live in `local-docs/.flow-cache/forge.json`. `flow sync` refreshes them, and so do online
`flow status`/`next`/`doctor`. The SessionStart hook reads only the cache and, when it is older than an hour, starts
`flow sync` in the background (10-minute lock, log `.flow-cache/sync.log`); the next session sees the fresh result.
`FLOW_BACKGROUND_SYNC=0` turns the background sync off.

| MR | Stage | Ball |
|---|---|---|
| no MR | local, from the file | me |
| an unresolved thread whose last comment is not mine (draft or not) | changes | me |
| Draft | review-wait | the author (I am the author → me) |
| approvals ≥ `approvals_required`, mergeable, no open threads | merge-wait | them |
| approved but not mergeable (conflict, pipeline, my own open threads) | changes | me |
| I wrote last in every open thread | review-wait | them |
| nobody has commented: I am the author / I am a reviewer | review-wait | them / me |
| merged | merged | me |
| closed | local, from the file (`test`) + `doctor` | me |
| parked (from the file) | parked | them |

Only resolvable threads count. Bot threads count too, because the forge can block the merge on them. When the computed
ball is wrong (you are waiting for an answer in chat, or on someone else's MR), `flow set ball them --why "..."`
pins it. A pin holds until the MR moves (state, draft, approvals, last comment); after that it silently stops
applying and `doctor` names it. `flow set ball auto` removes the pin.

## Journal

`flow log [KEY] [--since YYYY-MM-DD]` is the task's timeline from three sources: the branch reflog (commit, amend,
rebase, reset, merge) and `origin/<branch>` (push); MR events from the forge (created, merged, ready/draft, review
request, approval, assignment, human and bot comments); and manual `flow note` entries.

Mechanical events accumulate in `local-docs/tasks/<KEY>.events.jsonl` during `flow sync` and `flow log`,
deduplicated by id; `status` and the hook only read the journal. `flow clean` flushes the journal before
`branch -D` (which deletes the branch reflog) and moves it to `done/`. Manual notes carry only the "why": the
journal collects what happened and when.

## Who does what

| Who | Commands | How it is enforced |
|---|---|---|
| **Human only** | `flow start`, `flow clean`, `flow set stage parked`, `flow migrate`, `flow install-hook`; un-draft, approve, merge | refused when `CLAUDECODE=1` (the Bash environment inside Claude Code); `FLOW_HUMAN=1` overrides, for humans only |
| **Agent** | `flow note`, `flow next` (plan → refine → implement → test; from test/changes — gate, push, MR), `flow sync`, `flow set ball … --why`, updating `next` after every state change | agent discipline; the mechanics (commits, pushes, reviews) are journalled without the agent |
| **Automation** | SessionStart prints `flow status --brief` into the context; `flow status`/`flow next` read MR state from the forge | the hook `flow install-hook` adds |

The human makes four decisions per task: start, un-draft, merge, clean. The agent drives the rest.

A command typed with Claude Code's `!` prefix runs in the session's environment, so it carries `CLAUDECODE=1` and
is refused like the agent's own. From inside a session, a human runs `! FLOW_HUMAN=1 flow …`; the plain way is a
terminal of their own.

## Stages

```
plan → refine → implement → test → review-wait ⇄ changes → merge-wait → merged → cleaned
                                                                   ↘ parked (blocked_on required)
```

| Transition | Who | Precondition |
|---|---|---|
| plan → refine → implement → test | agent, `flow next` | none |
| test → review-wait | agent, `flow next` | the `gate` command is green in the worktree, the branch is pushed with nothing ahead, the worktree is clean; the MR is found by branch, otherwise a Draft `<KEY>: <title>` is created. The file keeps `stage: test` |
| review-wait ⇄ changes → merge-wait → merged | nobody: computed | per the table above; at these stages `flow next` only refreshes the cache and reports the stage |
| changes → review-wait | agent, `flow next` | gate + push, as above; the stage changes once the forge sees the reply |
| merged → cleaned | human, `flow clean` | MR merged, worktree clean |
| * → parked | human, `flow set stage parked --blocked-on …` | a reason is required |

When the forge is unreachable (`glab`/`gh` timeout), transitions that depend on it refuse with a reason;
`flow status` shows `!N?` (a yellow MR badge on the board).

## Cycle

1. **Start.** In the primary checkout:
   ```bash
   flow start PROJ-4801 short-slug --title "What we are doing"
   cd ../myrepo-4801 && claude
   ```
   This creates branch `PROJ-4801-short-slug` from `origin/main`, worktree `../myrepo-4801`, the `local-docs`
   symlink, and `local-docs/tasks/PROJ-4801.md` with `stage: plan`. A good first message to the agent: "read
   `local-docs/tasks/PROJ-4801.md`, stage plan — study the code, fill in the notes and `next`, don't write code".
2. **Refine → implement → test.** The agent works in the worktree. It records facts with
   `flow note "…" [KEY] --next "…"` (KEY goes before the flags; without it the key comes from the branch) and
   advances the stage with `flow next` when the stage really changed. `next` always describes the **next action**,
   not the one just done.
3. **Hand off for review.** `flow next` from `test` runs the gate, requires a push, and creates a Draft MR. While
   the MR is Draft the stage is computed as `review-wait` with the ball on me. Un-drafting and calling reviewers is
   the human's job; the hook reminds with an "un-draft !N" line.
4. **Review.** Stage and ball follow the MR: threads arrive — `changes`/me; I reply — `review-wait`/them;
   approval — `merge-wait`. After fixes, `flow next` runs the gate and the push again.
5. **Merge.** The human merges on the forge; after the next sync the task becomes `merged`.
6. **Close.** In the primary checkout: `flow clean PROJ-4801` checks that the MR is merged, removes the worktree and
   the branch, pulls `main`, and moves the file to `local-docs/tasks/done/`.
7. **Every session.** SessionStart puts the task table into the context (`*` marks the current branch's task),
   followed by the current task's full `next`, the commands waiting for the human, and an `overlaps:` block with
   three kinds of lines:
   - `KEY <- main moved under it` — `main` gained commits touching the branch's files: a candidate for a
     conflicting rebase;
   - `A <-> B: both change …` — two branches change the same files;
   - `… stacked …` — one branch is built on another.

   Overlaps are computed from committed work against `origin/<base>` as of the last fetch, offline. Shared but
   harmless files are excluded through `overlap_ignore:` (a glob list) in `flow.local.yml`.
8. **Board.** `flow board` (= `flow status --html --open`) rebuilds and opens `local-docs/flow-status.html`: a
   kanban (work / review / done / parked) with badges for stage, ball, age (red: ball with me and five or more days
   without an update), MR state (a link to the forge), and `blocked_on` chips linking to the cards of known keys. Click
   the counters to filter by "ball with me" or "hide inactive". With `jira_base:` in `flow.local.yml`, keys link to
   Jira. `flow status --json` is the same row model for your own renderers.
9. **Snapshot in notes.** `flow status --md --write [PATH] --offline` rewrites the block between
   `<!-- plan:snapshot:begin -->` and `<!-- plan:snapshot:end -->` in PATH (default `local-docs/PLAN.local.md`);
   the rest of that file stays hand-written.

## Don'ts

- Don't delete branches or worktrees by hand: use `flow clean`, which refuses to lose unpushed work.
- Don't set `mr:` by hand to an MR of another project: `flow` looks MRs up in this repository's project only.
- Don't use the `FLOW_HUMAN=1` override from an agent session.

## Configuration

`local-docs/flow.local.yml`, all fields optional:

```yaml
forge: github                      # gitlab | github; default: told from origin's host
base_branch: main                  # default: origin/HEAD
worktree_dir: ../myrepo-{n}        # default: ../<primary dir minus last -segment>-{n}
gate: make check BASE={base}       # the pre-review check, any shell command; no default
jira_base: https://jira.example.com
overlap_ignore: [".metrics/*"]
approvals_required: 1
```

`local-docs/` creates itself and is hidden from git through `.git/info/exclude` on the first `flow start`;
`.gitignore` needs no change. Without a `gate`, `flow next` from `test` refuses and suggests
`--no-gate --why "…"`. `forge:` (`gitlab` or `github`) overrides detection from `origin`'s host.

## Outputs for other tools

While it waits on the forge or git, flow shows a progress line on stderr (`gh: reading MRs 4/9`) and erases it
before printing. It appears only when stderr is a terminal, so pipes and the hook never see it; `FLOW_PROGRESS=0`
turns it off on a terminal too.

- `flow status --json` — the full row model; consumers read it by field name, and the key set is a tested contract.
- `flow status --tsv` — for shell pipelines that cut by column position; columns are only ever appended.
- `flow doctor [--fix]` — symlinks in every worktree (`--fix` relinks broken ones), branches without a task file,
  task files without a branch, a `worktree:` field that disagrees with where git has the branch checked out, a
  closed MR, a stale pin, legacy fields, a missing `local-docs/` in the primary checkout, forge CLI availability.

## Safeguards

- `flow clean` does not delete commits that are on no remote and not in the head of the merged MR, even with
  `--force-unmerged`; to do that deliberately, pass `--discard-commits`. A branch the forge deleted after the merge
  does not get in the way.
- `flow next` in review runs the gate and checks in the worktree where the branch is actually checked out, not the
  one the `worktree:` field names; if there is none (detached HEAD, rebase), it refuses. An unfinished
  merge/rebase/cherry-pick refuses before the gate. A conflict with `origin/<base>` is a warning listing the files,
  not a block. "Pushed" is checked with `git ls-remote`, not the tracking ref, which survives the branch's deletion
  on the server.
- A 403 from the forge CLI gets its own access message rather than "forge unavailable", and the process's other
  calls carry on.
- `title` and `next` are one line without control characters; `|` is escaped in `--md`.

## Migrating old task files

Older task files store `ball`, a post-MR stage, and `mr` alongside a branch; `doctor` calls these "legacy". The
human runs `flow sync`, `flow migrate --dry-run`, then `flow migrate --apply`. A post-MR stage becomes `test`; `mr`
alongside a branch and `ball` are removed. A diverging ball becomes a pin only for tasks without an MR; with an MR,
migrate prints the command, so the pin is set deliberately.

## Known gaps

1. **The gate can only be skipped as a whole.** When the gate fails through no fault of the branch (for example,
   the local toolchain is older than the project requires), the way out is `flow next --no-gate --why "…"`: the
   reason becomes a task note, but every other step also goes unchecked. Comparing against a baseline on the base
   branch does not work: to `flow` the gate is one opaque command, a typical runner stops at the first failing
   step, and an old breakage hides a new one. The fix, if skips become frequent: `gate:` as a list of steps,
   `--skip-step NAME --why`, and running every step to the end.
2. **`merged` with a `next` that describes something other than clean.** Detecting that means parsing free text,
   and the heuristic is unreliable; such a `next` is visible next to the `doctor` line about cleaning.
3. **Empty stale branches** (`+0/-28`) are not flagged "recreate from main"; this needs a decision on whether to
   highlight or recreate them.
4. **The journal is written by `sync`, `log`, and `clean`, not by the hook.** Without forge access and without
   `flow log`, a task's reflog reaches the journal only on the next of those. The reflog lives 90 days, so in
   practice nothing is lost.
