"""flow — deterministic task lifecycle: one git worktree per task, one task file per key.

State lives in `<primary>/local-docs/tasks/<KEY>.md` (YAML frontmatter + free notes), where
`<primary>` is the main checkout; worktrees see the same directory through a `local-docs` symlink.
Nothing here is committed: `local-docs/` is gitignored in every repo that uses this tool.
The task file keeps only what the forge cannot know; the MR (a PR on GitHub), the stages after it and the ball are
derived from the forge's answers -- GitLab through `glab`, GitHub through `gh` -- cached in
`<primary>/local-docs/.flow-cache/forge.json` so the SessionStart hook stays offline.

Human-only commands (`start`, `clean`, `migrate`, `set stage parked`, `install-hook`) refuse to run when
CLAUDECODE=1 is set — that is the Claude Code Bash environment. FLOW_HUMAN=1 overrides.

Per-repo overrides live in `<primary>/local-docs/flow.local.yml`:

    forge: github                      # gitlab | github; default: told from origin's host
    base_branch: main                  # default: origin/HEAD
    worktree_dir: ../myrepo-{n}        # default: ../<primary dir minus last -segment>-{n}
    gate: make check BASE={base}   # the pre-review check, any shell command; no default, `--no-gate --why` skips it
    jira_base: https://jira.example.com     # optional: keys in `status --html` link to /browse/<KEY>
    overlap_ignore: [".metrics/*"]          # optional: files whose overlap between branches is not a conflict
    approvals_required: 1                    # approvals that make an MR merge-wait

Run `flow --help`, or `flow guide`, for the user flow.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import datetime as dt
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Literal, Protocol

import yaml

STAGES = [
    "plan", "refine", "implement", "test",
    "review-wait", "changes", "merge-wait",
    "merged", "cleaned", "parked",
]
WORK_STAGES = ["plan", "refine", "implement", "test"]
INACTIVE = {"merged", "cleaned", "parked"}
# Stages the forge decides; the task file never stores them (a legacy file still may, until `flow migrate`).
FORGE_STAGES = {"review-wait", "changes", "merge-wait", "merged"}
STORED_STAGES = WORK_STAGES + ["parked"]
# GitLab system notes worth a journal line; the rest ("added 3 commits", "changed this line") is noise that the
# reflog or the diff already carries.
JOURNAL_SYSTEM_NOTES = re.compile(r"^(marked this merge request as|marked as a|requested review from|removed review "
                                  r"request|approved this merge request|unapproved this merge request|merged|closed|"
                                  r"reopened|assigned to|unassigned)")
NOTE_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}): (.*)$")
SYNC_WORKERS = 8  # glab costs ~2.5s a call, nearly all of it latency; sequential sync took 80s for 20 tasks
CACHE_STALE_SECONDS = 3600  # the hook starts a background `flow sync` when the forge cache is older
SYNC_LOCK_SECONDS = 600  # a started background sync is not started again for this long
BALL_NONE = "—"  # nobody holds the ball: cleaned, unassigned, or waiting on nothing in particular
BALLS = {"me", "them", BALL_NONE}
FIELDS = ["key", "title", "stage", "ball", "ball_pin", "blocked_on", "branch", "worktree", "mr", "next", "updated",
          "docs"]
OMIT_WHEN_NONE = {"ball", "ball_pin"}  # `ball` is legacy (derived now); `ball_pin` exists only while pinned
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
KEY_IN_BRANCH = re.compile(r"^([A-Z][A-Z0-9]+-\d+)")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
FORGE_TIMEOUT = 25
FORGE_NETWORK_ERRORS = ("i/o timeout", "dial tcp", "no such host", "connection refused", "error connecting to")
# 401 as a status token, not as digits inside an iid/path (`!401`, `/4012`).
FORGE_AUTH_ERROR = re.compile(r"(?<![\w!#/])401(?!\d)|Unauthorized|Bad credentials")
FORGE_FORBIDDEN = re.compile(r"(?<![\w!#/])403(?!\d)|Forbidden")
FORGE_NOT_FOUND = re.compile(r"(?<![\w!#/])404(?!\d)|Not Found")
# Per forge: its CLI, product name, what a change request is called, and how its number is written.
ForgeWords = collections.namedtuple("ForgeWords", "cli product noun prefix")
FORGES = {"gitlab": ForgeWords("glab", "GitLab", "MR", "!"), "github": ForgeWords("gh", "GitHub", "PR", "#")}
NEXT_WIDTH = 60  # `next` column cap in the terminal table
OVERLAP_FILES_SHOWN = 3  # files named per overlap line before "+N more"
MERGED_STALE_DAYS = 3  # `flow doctor` asks for `flow clean` once a merged task is this old
# `flow note` stamps the date itself; a note that starts with one would carry it twice.
LEADING_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*:?\s*")

EXIT_ERROR = 1
EXIT_PRECONDITION = 2
EXIT_HUMAN_ONLY = 3


class FlowError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def today() -> str:
    """ISO date. FLOW_TODAY=YYYY-MM-DD overrides the clock so tests and boards are reproducible."""
    forced = os.environ.get("FLOW_TODAY")
    if not forced:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(forced).isoformat()
    except ValueError as error:
        raise FlowError(f"FLOW_TODAY={forced!r} is not YYYY-MM-DD") from error


def expand(template: object, what: str, **fields: object) -> str:
    """`str.format` a config template, turning a stray brace or unknown name into a FlowError."""
    try:
        return str(template).format(**fields)
    except (KeyError, IndexError, ValueError) as error:
        raise FlowError(f"{what} {template!r}: bad placeholder ({error}); allowed: "
                        + ", ".join("{" + f + "}" for f in fields)) from error


def single_line(value: str, field: str) -> str:
    """`title` and `next` render as one table cell and one MR title; a control character breaks both."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FlowError(f"{field} must be a single line without control characters")
    return value


def split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def warn(message: str) -> None:
    sys.stderr.write(f"flow: {message}\n")


def run(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise FlowError(f"{args[0]} is not installed") from error


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """stdout of a git command; with `check=False` a failure reads as empty output."""
    result = run(["git", *args], cwd)
    if check and result.returncode != 0:
        raise FlowError(f"git {' '.join(args)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_ok(*args: str, cwd: Path | None = None) -> bool:
    """For git commands whose answer is the exit code (`merge-base --is-ancestor`, `cat-file -e`)."""
    return run(["git", *args], cwd).returncode == 0


def branch_exists(repo: "Repo", branch: str) -> bool:
    return f"refs/heads/{branch}" in repo.refs


def is_dirty(path: Path) -> bool:
    return bool(git("status", "--porcelain", cwd=path, check=False))


def require_human(action: str) -> None:
    if os.environ.get("CLAUDECODE") and not os.environ.get("FLOW_HUMAN"):
        raise FlowError(
            f"`flow {action}` is human-only; run it from your own terminal "
            "(FLOW_HUMAN=1 overrides, for humans only).",
            EXIT_HUMAN_ONLY,
        )


# --------------------------------------------------------------------------- repo


def url_host(url: str) -> str:
    """The host of a git remote URL: `https://h/…`, `ssh://user@h:port/…`, or scp-like `user@h:path`."""
    url = url.strip()
    if "://" in url:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    match = re.match(r"^(?:[^@/]+@)?([^:/]+):", url)
    return match.group(1).lower() if match else ""


def forge_for_host(host: str) -> str | None:
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


class Repo:
    def __init__(self, cwd: Path | None = None):
        cwd = (cwd or Path.cwd()).resolve()
        try:
            self.root = Path(git("rev-parse", "--show-toplevel", cwd=cwd))
        except FlowError as error:
            raise FlowError("not inside a git repository") from error
        common = Path(git("rev-parse", "--git-common-dir", cwd=self.root))
        if not common.is_absolute():
            common = self.root / common
        self.primary = common.resolve().parent
        self.local_docs = self.primary / "local-docs"
        self.tasks_dir = self.local_docs / "tasks"
        self.config = self._load_config()

    def _load_config(self) -> dict:
        path = self.local_docs / "flow.local.yml"
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise FlowError(f"{path}: invalid YAML ({error})") from error
        if not isinstance(data, dict):
            raise FlowError(f"{path} must be a mapping")
        return data

    @functools.cached_property
    def forge_name(self) -> str:
        """`forge:` from flow.local.yml, else guessed from origin's host; a host that names neither is an error."""
        configured = self.config.get("forge")
        if configured is not None:
            if configured not in FORGES:
                raise FlowError(f"flow.local.yml `forge` must be one of {', '.join(FORGES)}, not {configured!r}")
            return str(configured)
        url = git("remote", "get-url", "origin", cwd=self.primary, check=False)
        host = url_host(url)
        guessed = forge_for_host(host)
        if guessed is None:
            raise FlowError(f"cannot tell the forge from origin's host {host or url or '(no origin)'!r}; "
                            f"set `forge: gitlab` or `forge: github` in {self.local_docs / 'flow.local.yml'}")
        return guessed

    @property
    def words(self) -> ForgeWords:
        """How to name this repo's forge in output. An undetectable forge reads as GitLab here and fails only where
        a forge is actually called, so the offline hook still renders."""
        try:
            return FORGES[self.forge_name]
        except FlowError:
            return FORGES["gitlab"]

    @property
    def approvals_required(self) -> int:
        """Approvals that move review-wait -> merge-wait; neither forge's own rule is queryable per project."""
        value = self.config.get("approvals_required", 1)
        if not isinstance(value, int) or value < 1:
            raise FlowError("flow.local.yml `approvals_required` must be a positive integer")
        return value

    def overlap_ignored(self, path: str) -> bool:
        """`overlap_ignore:` globs in flow.local.yml -- files every branch touches without it meaning a conflict."""
        patterns = self.config.get("overlap_ignore") or []
        if not isinstance(patterns, list):
            raise FlowError("flow.local.yml `overlap_ignore` must be a list of glob patterns")
        return any(fnmatch.fnmatchcase(path, str(pattern)) for pattern in patterns)

    @functools.cached_property
    def base_branch(self) -> str:
        if self.config.get("base_branch"):
            return str(self.config["base_branch"])
        ref = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=self.primary, check=False)
        return ref.rsplit("/", 1)[-1] if ref else "main"

    @property
    def base_ref(self) -> str:
        return f"origin/{self.base_branch}"

    @functools.cached_property
    def refs(self) -> frozenset[str]:
        """Every local branch and origin tracking ref, from one `for-each-ref` instead of a `rev-parse` per question.

        Cached for the process; a command that creates or deletes a ref calls `forget_git_state()` right after.
        """
        out = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin", cwd=self.primary,
                  check=False)
        return frozenset(out.splitlines())

    def forget_git_state(self) -> None:
        for name in ("refs", "worktrees"):
            self.__dict__.pop(name, None)

    def worktree_dir(self, key: str) -> Path:
        template = self.config.get("worktree_dir")
        if not template:
            name = self.primary.name
            stem = name.rsplit("-", 1)[0] if "-" in name else name
            template = f"../{stem}-{{n}}"
        number = key.split("-", 1)[1]
        return (self.primary / expand(template, "worktree_dir", n=number, key=key)).resolve()

    def gate(self) -> str | None:
        """The pre-review check, from config only: guessing a project's gate would run somebody else's pipeline."""
        gate = self.config.get("gate")
        return str(gate) if gate not in (None, False, "") else None

    def current_key(self) -> str | None:
        branch = git("branch", "--show-current", cwd=self.root, check=False)
        match = KEY_IN_BRANCH.match(branch)
        return match.group(1) if match else None

    def branch_worktree(self, branch: str) -> Path | None:
        """Where git has `branch` checked out -- the truth the task's `worktree:` field can drift from."""
        return next((path for path, name in self.worktrees.items() if name == branch), None)

    def task_path(self, key: str, done: bool = False) -> Path:
        return (self.tasks_dir / "done" / f"{key}.md") if done else (self.tasks_dir / f"{key}.md")

    def task_files(self) -> list[Path]:
        return sorted(self.tasks_dir.glob("*.md")) if self.tasks_dir.exists() else []

    def resolve_worktree(self, meta: dict) -> Path:
        if not meta.get("worktree"):
            return self.primary
        return (self.primary / str(meta["worktree"])).resolve()

    @functools.cached_property
    def worktrees(self) -> dict[Path, str]:
        """Map worktree path -> branch, from `git worktree list --porcelain`; cached like `refs`."""
        out = git("worktree", "list", "--porcelain", cwd=self.primary)
        result: dict[Path, str] = {}
        path: Path | None = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):]).resolve()
                result[path] = ""
            elif line.startswith("branch ") and path is not None:
                result[path] = line[len("branch "):].removeprefix("refs/heads/")
        return result


# --------------------------------------------------------------------------- task files


def default_meta(key: str) -> dict:
    return {
        "key": key, "title": "", "stage": "plan", "ball": None, "ball_pin": None, "blocked_on": [],
        "branch": "", "worktree": "", "mr": None, "next": "", "updated": today(), "docs": [],
    }


def parse_task(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        raise FlowError(f"{path}: missing YAML frontmatter")
    try:
        raw = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise FlowError(f"{path}: invalid frontmatter YAML ({error})") from error
    if not isinstance(raw, dict):
        raise FlowError(f"{path}: frontmatter is not a mapping")
    if raw.get("key") and str(raw["key"]) != path.stem:
        raise FlowError(f"{path}: frontmatter key {raw['key']!r} does not match the filename")
    meta = default_meta(path.stem)
    meta.update({k: v for k, v in raw.items() if k in FIELDS})
    if isinstance(meta["updated"], (dt.date, dt.datetime)):
        meta["updated"] = meta["updated"].isoformat()[:10]
    meta["blocked_on"] = list(meta.get("blocked_on") or [])
    meta["docs"] = list(meta.get("docs") or [])
    if meta["stage"] not in STAGES:
        raise FlowError(f"{path}: unknown stage {meta['stage']!r}")
    return meta, match.group(2)


def dump_task(path: Path, meta: dict, body: str) -> None:
    ordered = {field: meta.get(field) for field in FIELDS
               if not (field in OMIT_WHEN_NONE and meta.get(field) is None)}
    front = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body.rstrip("\n")
    path.write_text(f"---\n{front}---\n{body}\n" if body else f"---\n{front}---\n", encoding="utf-8")


def load_task(repo: Repo, key: str) -> tuple[Path, dict, str]:
    path = repo.task_path(key)
    if not path.exists():
        done = repo.task_path(key, done=True)
        if done.exists():
            raise FlowError(f"{key} is closed ({done}); reopen with a new `flow start` if needed")
        raise FlowError(f"no task file for {key} at {path}")
    meta, body = parse_task(path)
    return path, meta, body


def save_task(path: Path, meta: dict, body: str) -> None:
    meta["updated"] = today()
    dump_task(path, meta, body)


def pick_key(repo: Repo, key: str | None, action: str) -> str:
    if key:
        if not KEY_RE.match(key):
            raise FlowError(f"{key!r} is not a task key (expected e.g. PL-1234)")
        return key
    current = repo.current_key()
    if not current:
        raise FlowError(f"`flow {action}` needs a KEY: the current branch carries none")
    return current


# --------------------------------------------------------------------------- forges


class Forge(Protocol):
    """What flow asks a forge. Every MR it returns is normalized by `neutral_mr` (plus `iid`), so the cache, the
    derivation of stage and ball, and every renderer never see a GitLab or GitHub payload."""

    name: str
    cli: str

    def username(self) -> str: ...
    def latest_mr_for_branch(self, branch: str) -> dict | None: ...
    def mrs_by_iid(self, iids: list[int]) -> dict[int, dict]: ...
    def mr_view(self, iid: int) -> dict: ...
    def details(self, iid: int, entry: dict, is_open: bool) -> dict: ...  # approvals, threads, events, maybe more
    def mr_create(self, branch: str, base: str, title: str) -> int: ...


def neutral_mr(iid: int, *, state: str, draft: bool, author: str, source_branch: str, sha: str, merge_status: str,
               web_url: str, created_at: str, merged_at: str) -> dict:
    """The one MR shape the cache stores. `merge_status` is `mergeable` exactly when the forge would merge now."""
    return {"iid": iid, "state": state, "draft": draft, "author": author, "source_branch": source_branch, "sha": sha,
            "merge_status": merge_status, "web_url": web_url, "merged_at": merged_at[:10], "approvals": 0,
            "threads": [], "created_at": created_at, "merged_at_full": merged_at, "events": []}


def login(value: object, field: str = "username") -> str:
    return str(value.get(field) or "") if isinstance(value, dict) else ""


def lifecycle_events(prefix: str, ref: str, entry: dict) -> list[dict]:
    """MR created / merged, from the MR itself: both forges report them, neither as a timeline item flow keeps."""
    events = []
    for kind, at in (("mr-created", entry.get("created_at")), ("mr-merged", entry.get("merged_at_full"))):
        ts = iso_ts(at or "")
        if ts:
            events.append({"id": f"{prefix}:{ref}:{kind}", "ts": ts, "kind": kind, "text": ref})
    return events


class ForgeCli:
    """A forge reached through its CLI: one subprocess per call, and a connectivity or auth failure remembered, so
    the rest of the process fails at once instead of waiting out the same timeout again."""

    name = ""
    cli = ""

    def __init__(self, root: Path):
        self.root = root
        self.dead: FlowError | None = None

    def _fail(self, message: str) -> FlowError:
        self.dead = FlowError(message, EXIT_PRECONDITION)
        return self.dead

    def _run(self, args: list[str], missing_ok: bool = False) -> str | None:
        """stdout; `None` for a 404 when `missing_ok`, which is an answer ("no such MR"), not a failure."""
        if self.dead:
            raise self.dead
        try:
            result = subprocess.run([self.cli, *args], cwd=self.root, capture_output=True, text=True,
                                    timeout=FORGE_TIMEOUT)
        except FileNotFoundError as error:
            raise self._fail(f"{self.cli} is not installed") from error
        except subprocess.TimeoutExpired as error:
            raise self._fail(f"{self.cli} offline: timed out after {FORGE_TIMEOUT}s (VPN?)") from error
        if result.returncode != 0:
            err = result.stderr.strip()
            if any(marker in err for marker in FORGE_NETWORK_ERRORS):
                raise self._fail(f"{self.cli} offline: {err.splitlines()[-1] if err else 'unreachable (VPN?)'}")
            if FORGE_AUTH_ERROR.search(err):
                raise self._fail(f"{self.cli} auth: 401 — run `{self.cli} auth login`")
            if FORGE_FORBIDDEN.search(err):
                # One endpoint refusing is not the forge going away: later calls may still be allowed.
                raise FlowError(f"{self.cli}: 403 — the token lacks scope or project access changed "
                                f"({err.splitlines()[-1]})", EXIT_PRECONDITION)
            if missing_ok and FORGE_NOT_FOUND.search(err):
                return None
            raise FlowError(f"{self.cli}: {err or 'failed'}", EXIT_PRECONDITION)
        return result.stdout

    def api(self, path: str, *fields: str, missing_ok: bool = False) -> object:
        out = self._run(["api", path, *fields], missing_ok=missing_ok)
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as error:
            raise FlowError(f"{self.cli}: non-JSON response for {path}", EXIT_PRECONDITION) from error


class Glab(ForgeCli):
    """GitLab through `glab api`, which resolves `:id` to the checkout's project."""

    name, cli = "gitlab", "glab"

    @staticmethod
    def _mr(data: dict) -> dict:
        return neutral_mr(
            int(data["iid"]), state=str(data.get("state", "?")), draft=bool(data.get("draft")),
            author=login(data.get("author")), source_branch=str(data.get("source_branch") or ""),
            sha=str(data.get("sha") or ""), merge_status=str(data.get("detailed_merge_status") or ""),
            web_url=str(data.get("web_url") or ""), created_at=str(data.get("created_at") or ""),
            merged_at=str(data.get("merged_at") or ""))

    def mr_view(self, iid: int) -> dict:
        data = self.api(f"projects/:id/merge_requests/{iid}")
        if not isinstance(data, dict):
            raise FlowError(f"glab: unexpected payload for !{iid}", EXIT_PRECONDITION)
        return self._mr(data)

    def mrs_by_iid(self, iids: list[int]) -> dict[int, dict]:
        """One request per 50 iids instead of one per MR; an iid the API does not return is simply absent."""
        found: dict[int, dict] = {}
        for start in range(0, len(iids), 50):
            query = "&".join(f"iids[]={iid}" for iid in iids[start:start + 50])
            data = self.api(f"projects/:id/merge_requests?{query}&per_page=50")
            if isinstance(data, list):
                found.update({int(d["iid"]): self._mr(d) for d in data if isinstance(d, dict) and "iid" in d})
        return found

    def _open_mr_for_branch(self, branch: str) -> dict | None:
        data = self.api(f"projects/:id/merge_requests?source_branch={quote(branch)}&state=opened&per_page=5")
        return data[0] if isinstance(data, list) and data else None

    def latest_mr_for_branch(self, branch: str) -> dict | None:
        """Any state: a merged or closed MR is still the branch's MR. Newest first, so a reopened branch wins."""
        data = self.api(f"projects/:id/merge_requests?source_branch={quote(branch)}&state=all"
                        "&order_by=created_at&sort=desc&per_page=5")
        return self._mr(data[0]) if isinstance(data, list) and data else None

    def details(self, iid: int, entry: dict, is_open: bool) -> dict:
        """Approvals (open MRs only: a settled one's count moves nothing) and the discussions behind threads."""
        approvals = 0
        if is_open:
            data = self.api(f"projects/:id/merge_requests/{iid}/approvals")
            approvals = len(data.get("approved_by", [])) if isinstance(data, dict) else 0
        data = self.api(f"projects/:id/merge_requests/{iid}/discussions?per_page=100")
        discussions = data if isinstance(data, list) else []
        return {"approvals": approvals, "threads": self._threads(discussions),
                "events": lifecycle_events("gl", f"!{iid}", entry) + self._events(iid, discussions)}

    @staticmethod
    def _threads(discussions: list) -> list[dict]:
        """Resolvable review threads only: a plain comment carries no obligation that a reply discharges."""
        threads = []
        for discussion in discussions:
            notes = [n for n in (discussion.get("notes") or []) if isinstance(n, dict) and not n.get("system")]
            resolvable = [n for n in notes if n.get("resolvable")]
            if not resolvable:
                continue
            threads.append({"resolved": all(n.get("resolved") for n in resolvable),
                            "last_author": login(notes[-1].get("author")),
                            "last_note_id": int(notes[-1].get("id") or 0)})
        return threads

    @staticmethod
    def _events(iid: int, discussions: list) -> list[dict]:
        """Journal lines from notes: review requests, approvals and state changes, and human comments."""
        events = []
        for discussion in discussions:
            for note in discussion.get("notes") or []:
                if not isinstance(note, dict) or not note.get("id"):
                    continue
                ts = iso_ts(str(note.get("created_at") or ""))
                author = login(note.get("author")) or "?"
                body = " ".join(str(note.get("body") or "").replace("**", "").split())
                if note.get("system"):
                    if ts and JOURNAL_SYSTEM_NOTES.match(body):
                        events.append({"id": f"gl:{note['id']}", "ts": ts, "kind": "gitlab",
                                       "text": f"!{iid} {author} {truncate(body, 100)}"})
                elif ts:
                    events.append({"id": f"gl:{note['id']}", "ts": ts, "kind": "comment",
                                   "text": f"!{iid} {author}: {truncate(body, 100)}"})
        return events

    def username(self) -> str:
        name = login(self.api("user"))
        if not name:
            raise FlowError("glab: `user` returned no username", EXIT_PRECONDITION)
        return name

    def mr_create(self, branch: str, base: str, title: str) -> int:
        out = self._run(["mr", "create", "--draft", "--title", title, "--description", "",
                         "--source-branch", branch, "--target-branch", base, "--yes"]) or ""
        printed = re.search(r"/-/merge_requests/(\d+)", out)  # glab prints the new MR's URL
        if printed:
            return int(printed.group(1))
        created = self._open_mr_for_branch(branch)
        if not created:
            raise FlowError("glab: MR created but not found by source branch", EXIT_PRECONDITION)
        return int(created["iid"])


# One round trip per PR for everything REST would need five calls for. `latestReviews` holds each reviewer's
# latest verdict; `reviewThreads` are the resolvable conversations; the timeline feeds the journal.
GH_PR_QUERY = """query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      isDraft mergeStateStatus
      commits(last: 1) { nodes { commit { committedDate } } }
      latestReviews(first: 50) { nodes { databaseId state submittedAt author { login } } }
      reviewThreads(first: 100) { nodes { isResolved comments(last: 1) { nodes { databaseId author { login } } } } }
      timelineItems(first: 100, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REVIEW_REQUESTED_EVENT,
                                            PULL_REQUEST_REVIEW, MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT,
                                            ASSIGNED_EVENT, ISSUE_COMMENT]) {
        nodes {
          __typename
          ... on ReadyForReviewEvent { id createdAt actor { login } }
          ... on ConvertToDraftEvent { id createdAt actor { login } }
          ... on ReviewRequestedEvent { id createdAt actor { login }
                                        requestedReviewer { ... on User { login } ... on Team { name } } }
          ... on PullRequestReview { id submittedAt state body author { login } }
          ... on MergedEvent { id createdAt actor { login } }
          ... on ClosedEvent { id createdAt actor { login } }
          ... on ReopenedEvent { id createdAt actor { login } }
          ... on AssignedEvent { id createdAt actor { login } assignee { ... on User { login } } }
          ... on IssueComment { id createdAt body author { login } }
        }
      }
    }
  }
}"""
GH_TIMELINE_VERBS = {"ReadyForReviewEvent": "marked as ready", "ConvertToDraftEvent": "marked as draft",
                     "MergedEvent": "merged", "ClosedEvent": "closed", "ReopenedEvent": "reopened"}
GH_REVIEW_VERBS = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "DISMISSED": "review dismissed"}


class Gh(ForgeCli):
    """GitHub through `gh api`, which fills `{owner}`/`{repo}` from the checkout and follows its host (Enterprise)."""

    name, cli = "github", "gh"

    @staticmethod
    def _mr(data: dict) -> dict:
        merged_at = str(data.get("merged_at") or "")
        state = "merged" if merged_at else ("opened" if data.get("state") == "open" else "closed")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        mergeable = str(data.get("mergeable_state") or "")  # only single-PR reads carry it; `details` refreshes it
        return neutral_mr(
            int(data["number"]), state=state, draft=bool(data.get("draft")), author=login(data.get("user"), "login"),
            source_branch=str(head.get("ref") or ""), sha=str(head.get("sha") or ""),
            merge_status="mergeable" if mergeable == "clean" else mergeable, web_url=str(data.get("html_url") or ""),
            created_at=str(data.get("created_at") or ""), merged_at=merged_at)

    def mr_view(self, iid: int) -> dict:
        data = self.api(f"repos/{{owner}}/{{repo}}/pulls/{iid}")
        if not isinstance(data, dict):
            raise FlowError(f"gh: unexpected payload for #{iid}", EXIT_PRECONDITION)
        return self._mr(data)

    def mrs_by_iid(self, iids: list[int]) -> dict[int, dict]:
        """GitHub has no batch read by number over REST; the calls fan out instead. A 404 is simply absent."""
        answers = parallel([lambda i=i: self.api(f"repos/{{owner}}/{{repo}}/pulls/{i}", missing_ok=True)
                            for i in iids])
        return {iid: self._mr(data) for iid, data in zip(iids, answers) if isinstance(data, dict)}

    def latest_mr_for_branch(self, branch: str) -> dict | None:
        data = self.api(f"repos/{{owner}}/{{repo}}/pulls?head={{owner}}:{quote(branch)}&state=all"
                        "&sort=created&direction=desc&per_page=5")
        return self._mr(data[0]) if isinstance(data, list) and data else None

    def details(self, iid: int, entry: dict, is_open: bool) -> dict:
        data = self.api("graphql", "-f", f"query={GH_PR_QUERY}", "-F", "owner={owner}", "-F", "repo={repo}",
                        "-F", f"number={iid}")
        pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") if isinstance(data, dict) \
            else None
        if not isinstance(pr, dict):
            raise FlowError(f"gh: no pull request #{iid} in the GraphQL answer", EXIT_PRECONDITION)
        reviews = nodes(pr.get("latestReviews"))
        status = str(pr.get("mergeStateStatus") or "")
        return {"approvals": sum(r.get("state") == "APPROVED" for r in reviews) if is_open else 0,
                "threads": self._threads(pr, reviews, entry["author"]),
                "events": lifecycle_events("gh", f"#{iid}", entry) + self._events(iid, nodes(pr.get("timelineItems"))),
                "draft": bool(pr.get("isDraft")),
                "merge_status": "mergeable" if status == "CLEAN" else status.lower()}

    @staticmethod
    def _threads(pr: dict, reviews: list[dict], author: str) -> list[dict]:
        """Review threads, plus one open thread per standing "changes requested" verdict.

        A verdict has nothing to resolve, and GitHub keeps it until the reviewer reviews again. Until the author
        pushes after it, the reviewer spoke last (the author owes the change); after the push the author did (the
        reviewer owes the re-review). `forge_stage` then reads it like any other thread.
        """
        threads = []
        for thread in nodes(pr.get("reviewThreads")):
            last = (nodes(thread.get("comments")) or [{}])[-1]
            threads.append({"resolved": bool(thread.get("isResolved")), "last_author": login(last.get("author"), "login"),
                            "last_note_id": int(last.get("databaseId") or 0)})
        head = iso_ts(str(((nodes(pr.get("commits")) or [{}])[-1].get("commit") or {}).get("committedDate") or ""))
        for review in reviews:
            if review.get("state") != "CHANGES_REQUESTED":
                continue
            at = iso_ts(str(review.get("submittedAt") or ""))
            answered = head is not None and at is not None and head > at
            threads.append({"resolved": False,
                            "last_author": author if answered else login(review.get("author"), "login"),
                            "last_note_id": int(review.get("databaseId") or 0)})
        return threads

    @staticmethod
    def _events(iid: int, items: list[dict]) -> list[dict]:
        events = []
        for item in items:
            kind = item.get("__typename")
            ts = iso_ts(str(item.get("createdAt") or item.get("submittedAt") or ""))
            if not ts or not item.get("id"):
                continue
            actor = login(item.get("actor") or item.get("author"), "login") or "?"
            body = " ".join(str(item.get("body") or "").split())
            if kind in GH_TIMELINE_VERBS:
                text = f"#{iid} {actor} {GH_TIMELINE_VERBS[kind]}"
            elif kind == "ReviewRequestedEvent":
                who = item.get("requestedReviewer") or {}
                text = f"#{iid} {actor} requested review from {who.get('login') or who.get('name') or '?'}"
            elif kind == "AssignedEvent":
                text = f"#{iid} {actor} assigned to {login(item.get('assignee'), 'login') or '?'}"
            elif kind == "PullRequestReview" and item.get("state") in GH_REVIEW_VERBS:
                text = f"#{iid} {actor} {GH_REVIEW_VERBS[item['state']]}" + (f": {truncate(body, 100)}" if body else "")
            elif kind in ("PullRequestReview", "IssueComment") and body:
                events.append({"id": f"gh:{item['id']}", "ts": ts, "kind": "comment",
                               "text": f"#{iid} {actor}: {truncate(body, 100)}"})
                continue
            else:
                continue
            events.append({"id": f"gh:{item['id']}", "ts": ts, "kind": "github", "text": text})
        return events

    def username(self) -> str:
        name = login(self.api("user"), "login")
        if not name:
            raise FlowError("gh: `user` returned no login", EXIT_PRECONDITION)
        return name

    def mr_create(self, branch: str, base: str, title: str) -> int:
        out = self._run(["pr", "create", "--draft", "--title", title, "--body", "", "--head", branch,
                         "--base", base]) or ""
        printed = re.search(r"/pull/(\d+)", out)  # gh prints the new PR's URL
        if printed:
            return int(printed.group(1))
        created = self.latest_mr_for_branch(branch)
        if not created or created["state"] != "opened":
            raise FlowError("gh: PR created but not found by head branch", EXIT_PRECONDITION)
        return int(created["iid"])


def nodes(connection: object) -> list[dict]:
    """A GraphQL connection's `nodes`, tolerating null at every level."""
    found = connection.get("nodes") if isinstance(connection, dict) else None
    return [n for n in (found or []) if isinstance(n, dict)]


def make_forge(repo: Repo) -> Forge:
    return {"gitlab": Glab, "github": Gh}[repo.forge_name](repo.root)


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


@dataclasses.dataclass(frozen=True)
class MrFacts:
    """What a row shows about the task's MR. `label` tells "never asked" (`!5`) from "asked, no answer" (`!5?`)."""

    iid: int | None
    state: str  # none | unknown | opened | merged | closed | locked
    draft: bool
    url: str
    label: str


def mr_facts(iid: int | None, entry: dict | None, asked: bool, prefix: str = "!") -> MrFacts:
    if iid is None:
        return MrFacts(None, "none", False, "", "-")
    if entry is None:
        return MrFacts(iid, "unknown", False, "", f"{prefix}{iid}{'?' if asked else ''}")
    state, draft = str(entry.get("state", "?")), bool(entry.get("draft"))
    return MrFacts(iid, state, draft, str(entry.get("web_url") or ""), f"{prefix}{iid}:{state}{'D' if draft else ''}")


# --------------------------------------------------------------------------- forge cache and derivation


class ForgeCache:
    """The forge's answers, kept so the offline hook can derive stage and ball without the network.

    One file for the primary and all worktrees (it lives under the shared `local-docs/`), written by replacing it
    whole, so a concurrent reader sees either the old or the new version.
    """

    def __init__(self, repo: Repo):
        self.path = repo.local_docs / ".flow-cache" / "forge.json"
        self.data: dict = {"fetched_at": None, "me": None, "by_branch": {}, "mrs": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                warn(f"ignoring unreadable {self.path} ({error})")
            else:
                if isinstance(loaded, dict):
                    self.data.update(loaded)

    @classmethod
    def from_data(cls, data: dict) -> "ForgeCache":
        """A cache over given facts, touching no file: for tests and for callers that already hold the data."""
        cache = cls.__new__(cls)
        cache.path = Path(os.devnull)
        cache.data = {"fetched_at": None, "me": None, "by_branch": {}, "mrs": {}, **data}
        return cache

    @property
    def synced(self) -> bool:
        return self.data.get("fetched_at") is not None

    def age_seconds(self) -> float | None:
        return time.time() - float(self.data["fetched_at"]) if self.synced else None

    def iid_for(self, meta: dict) -> int | None:
        """A stored `mr:` (branchless tasks) wins; otherwise the MR the forge has for the task's branch."""
        if meta.get("mr"):
            return int(meta["mr"])
        found = self.data["by_branch"].get(meta.get("branch") or "")
        return int(found) if found else None

    def mr(self, iid: int | None) -> dict | None:
        return self.data["mrs"].get(str(iid)) if iid else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def iso_ts(value: str) -> int | None:
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parallel(calls: list) -> list:
    """Run zero-argument callables on a thread pool, results in order; the first FlowError propagates."""
    if not calls:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
        return list(pool.map(lambda call: call(), calls))


def sync(repo: Repo, metas: list[dict], cache: ForgeCache, forge: Forge, prune: bool) -> None:
    """Ask the forge about every task's MR and store the answers; `prune` also drops MRs no task refers to any more.

    Two parallel rounds: every lookup that needs nothing but the task file, then the detail of each MR found.
    """
    if cache.data.get("forge") not in (None, forge.name):  # origin moved to another forge: its iids mean nothing here
        cache.data = ForgeCache.from_data({}).data
    live = [m for m in metas if m["stage"] != "cleaned"]
    branches = sorted({m["branch"] for m in live if m["branch"]})  # even with a stored `mr`: doctor compares
    stored = sorted({int(m["mr"]) for m in live if m.get("mr")})
    lookups: dict[object, object] = {("branch", b): (lambda b=b: forge.latest_mr_for_branch(b)) for b in branches}
    if not cache.data.get("me"):
        lookups["me"] = forge.username
    if stored:
        lookups["stored"] = lambda: forge.mrs_by_iid(stored)
    found = dict(zip(lookups, parallel(list(lookups.values()))))
    if "me" in found:
        cache.data["me"] = found["me"]
    raw: dict[int, dict] = dict(found.get("stored") or {})  # normalized MRs, each still carrying its `iid`
    by_branch = {} if prune else dict(cache.data["by_branch"])
    for branch in branches:
        by_branch.pop(branch, None)
        data = found[("branch", branch)]
        if data:
            by_branch[branch] = int(data["iid"])
            raw[int(data["iid"])] = data
    entries = {iid: {k: v for k, v in data.items() if k != "iid"} for iid, data in raw.items()}
    known = cache.data["mrs"]
    # An open MR changes all the time; a closed or merged one is read once more when it settles, then kept.
    wanted = {iid: entry["state"] == "opened" for iid, entry in entries.items()
              if entry["state"] == "opened" or known.get(str(iid), {}).get("state") != entry["state"]}
    details = dict(zip(wanted, parallel([lambda i=i, o=is_open: forge.details(i, entries[i], o)
                                         for i, is_open in wanted.items()])))
    for iid, entry in entries.items():
        if iid in details:
            entry.update(details[iid])
        else:
            entry["events"] = known.get(str(iid), {}).get("events", [])
    mrs = {} if prune else dict(known)
    mrs.update({str(iid): entry for iid, entry in entries.items()})
    for iid in stored:
        if iid not in raw:
            mrs.pop(str(iid), None)  # asked, and the forge has no such MR: unknown, not stale
    cache.data.update(forge=forge.name, fetched_at=time.time(), by_branch=by_branch, mrs=mrs)
    cache.save()


def fingerprint(entry: dict | None) -> str:
    """What a ball pin is valid for: when any of this moves, the reason for the pin is presumed gone."""
    if entry is None:
        return "no-mr"
    last = max((t["last_note_id"] for t in entry.get("threads", [])), default=0)
    return f"{entry['state']}|{int(entry['draft'])}|{entry.get('approvals', 0)}|{last}"


def forge_stage(entry: dict, me: str, approvals_required: int) -> tuple[str, str]:
    """(stage, ball) for an open MR. Symmetric in role: whoever did not write last in an open thread owes a reply."""
    mine = entry["author"] == me
    open_threads = [t for t in entry["threads"] if not t["resolved"]]
    if any(t["last_author"] != me for t in open_threads):
        return "changes", "me"  # a reply is owed, draft or not
    if entry["draft"]:
        return "review-wait", "me" if mine else "them"  # the author un-drafts
    approved = entry.get("approvals", 0) >= approvals_required
    if approved and entry["merge_status"] == "mergeable" and not open_threads:
        return "merge-wait", "them"
    if approved:
        return "changes", "me"  # approved but not mergeable: conflicts, pipeline, or open threads of mine
    if open_threads:
        return "review-wait", "them"  # I wrote last in every open thread
    return "review-wait", "them" if mine else "me"  # nobody has spoken: the reviewer owes the first pass


@dataclasses.dataclass(frozen=True)
class Derived:
    """A task's effective state: what the file stores, overridden by what the cached MR says."""

    stage: str
    ball: str
    iid: int | None
    entry: dict | None  # the cached MR, if the forge has been asked about it
    stage_source: Literal["stored", "forge"]
    ball_source: Literal["derived", "pin"]
    closed: bool  # the MR was closed unmerged: the stage falls back to the file's
    pin_stale: bool  # a ball pin exists but the MR has moved since it was set
    fingerprint: str


def derive(meta: dict, cache: ForgeCache, approvals_required: int) -> Derived:
    """Effective stage and ball: the MR decides forge stages, the file keeps only what the forge cannot know."""
    stored = meta["stage"]
    iid = cache.iid_for(meta)
    entry = cache.mr(iid)
    source: Literal["stored", "forge"] = "stored"
    closed = False
    if stored == "cleaned":
        stage, ball = "cleaned", BALL_NONE
    elif stored == "parked":
        stage, ball = "parked", "them"  # parking needs `blocked_on`: someone else holds it
    elif entry is None:
        stage, ball = stored, meta.get("ball") or "me"  # legacy stored ball until `flow migrate`; else it is mine
    elif entry["state"] == "merged":
        stage, ball, source = "merged", "me", "forge"
    elif entry["state"] == "closed":
        stage, ball, closed = (stored if stored in STORED_STAGES else "test"), "me", True
    else:
        stage, ball = forge_stage(entry, str(cache.data.get("me") or ""), approvals_required)
        source = "forge"
    stamp = fingerprint(entry)
    pin = meta.get("ball_pin")
    pinned = stored != "cleaned" and isinstance(pin, dict) and pin.get("value") in BALLS
    fresh = pinned and pin.get("fingerprint") == stamp
    return Derived(stage=stage, ball=pin["value"] if fresh else ball, iid=iid, entry=entry, stage_source=source,
                   ball_source="pin" if fresh else "derived", closed=closed, pin_stale=pinned and not fresh,
                   fingerprint=stamp)


# --------------------------------------------------------------------------- journal


def events_path(repo: Repo, key: str, done: bool = False) -> Path:
    return (repo.tasks_dir / "done" if done else repo.tasks_dir) / f"{key}.events.jsonl"


GIT_KINDS = (("commit (amend)", "amend"), ("commit", "commit"), ("rebase", "rebase"), ("reset", "reset"),
             ("merge", "merge"), ("pull", "pull"), ("cherry-pick", "cherry-pick"), ("branch", "branch"),
             ("update by push", "push"), ("fetch", "fetch"))


def git_events(repo: Repo, branch: str) -> list[dict]:
    """The branch's own reflog (commits, amends, rebases, resets) and its origin copy's (pushes, fetches).

    Offline and cheap. It must be read before `flow clean`: `git branch -D` deletes the branch reflog with it.
    """
    events = []
    for ref, prefix in ((f"refs/heads/{branch}", "local"), (f"refs/remotes/origin/{branch}", "origin")):
        out = git("reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", ref, "--", cwd=repo.primary,
                  check=False)
        for line in out.splitlines():
            parts = line.split("\t", 2)
            stamp = re.search(r"@\{(\d+)\}$", parts[1]) if len(parts) == 3 else None
            if not stamp:
                continue
            sha, subject = parts[0], parts[2]
            kind = next((k for marker, k in GIT_KINDS if subject.startswith(marker)), "git")
            if kind == "fetch":
                continue  # a fetch moves nothing of mine
            events.append({"id": f"{prefix}:{stamp.group(1)}:{sha[:12]}", "ts": int(stamp.group(1)), "kind": kind,
                           "text": f"{sha[:8]} {truncate(subject, 100)}"})
    return events


def read_events(path: Path) -> list[dict]:
    """Every event once, oldest first; a torn or duplicated line (two sessions appending) is tolerated."""
    seen: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "id" in event and "ts" in event:
                seen.setdefault(str(event["id"]), event)
    return sorted(seen.values(), key=lambda e: (e["ts"], e["id"]))


def collect_events(repo: Repo, meta: dict, cache: ForgeCache) -> list[dict]:
    """Append what git and the cached forge facts know and the journal does not yet; return the whole journal."""
    path = events_path(repo, meta["key"])
    have = read_events(path)
    ids = {e["id"] for e in have}
    fresh = git_events(repo, meta["branch"]) if meta["branch"] and branch_exists(repo, meta["branch"]) else []
    entry = cache.mr(cache.iid_for(meta))
    fresh += entry.get("events", []) if entry else []
    new = sorted((e for e in fresh if e["id"] not in ids), key=lambda e: (e["ts"], e["id"]))
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in new))
    return sorted(have + new, key=lambda e: (e["ts"], e["id"]))


def note_entries(body: str) -> list[tuple[str, str]]:
    """Hand-written `- YYYY-MM-DD: ...` notes, continuation lines folded into their note."""
    notes: list[tuple[str, str]] = []
    for line in body.splitlines():
        match = NOTE_LINE.match(line)
        if match:
            notes.append((match.group(1), match.group(2)))
        elif notes and line.startswith("  ") and line.strip():
            notes[-1] = (notes[-1][0], notes[-1][1] + " " + line.strip())
    return notes


def cmd_log(repo: Repo, args: argparse.Namespace) -> int:
    key = pick_key(repo, args.key, "log")
    path, meta, body = load_task(repo, key)
    events = collect_events(repo, meta, ForgeCache(repo))
    lines = [(dt.datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M"), e["kind"], e["text"]) for e in events]
    lines += [(f"{day}      ", "note", text) for day, text in note_entries(body)]
    lines.sort(key=lambda line: line[0])
    if args.since:
        lines = [line for line in lines if line[0][:10] >= args.since]
    for when, kind, text in lines:
        print(f"{when}  {kind:<11} {text}")
    return 0


def human_age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "just now" if seconds < 1 else f"{int(seconds)}s"


def start_background_sync(repo: Repo, cache: ForgeCache) -> bool:
    """Refresh a stale cache for the next session without making this one wait on the network."""
    if os.environ.get("FLOW_BACKGROUND_SYNC") == "0":
        return False
    age = cache.age_seconds()
    if age is not None and age < CACHE_STALE_SECONDS:
        return False
    lock = cache.path.parent / "sync.lock"
    try:
        if time.time() - lock.stat().st_mtime < SYNC_LOCK_SECONDS:
            return False
    except FileNotFoundError:
        pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    with open(lock.parent / "sync.log", "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, "-m", "flow_worktrees", "sync", "--quiet"], cwd=repo.root,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return True


# --------------------------------------------------------------------------- status


def branch_ab(repo: Repo, branch: str) -> str:
    if not branch:
        return "-"
    if not branch_exists(repo, branch):
        return "gone"
    counts = git("rev-list", "--left-right", "--count", f"{branch}...{repo.base_ref}",
                 cwd=repo.primary, check=False)
    if not counts:
        return "?"
    ahead, behind = counts.split()
    return f"+{ahead}/-{behind}{push_state(repo, branch)}"


def push_state(repo: Repo, branch: str) -> str:
    """Where the branch stands against its own copy on origin: `` in sync, ` local` never pushed, `⇡N`/`⇣N` apart.

    Ahead/behind against the base does not say whether a rebase is safe; this does.
    """
    remote = f"refs/remotes/origin/{branch}"
    if remote not in repo.refs:
        return " local"
    counts = git("rev-list", "--left-right", "--count", f"{branch}...{remote}", cwd=repo.primary, check=False)
    if not counts:
        return " ?"
    unpushed, unpulled = (int(n) for n in counts.split())
    return (f" ⇡{unpushed}" if unpushed else "") + (f"⇣{unpulled}" if unpulled else "")


def worktree_label(repo: Repo, meta: dict) -> str:
    if not meta.get("worktree"):
        return "primary"
    path = repo.resolve_worktree(meta)
    if not path.exists():
        return "missing"
    return "dirty" if is_dirty(path) else "ok"


def age_int(updated: str) -> int | None:
    try:
        return (dt.date.fromisoformat(today()) - dt.date.fromisoformat(str(updated)[:10])).days
    except ValueError:
        return None


def age_days(updated: str) -> str:
    days = age_int(updated)
    if days is None:
        return "?"
    return "today" if days == 0 else f"{days}d"


def load_tasks(repo: Repo) -> list[dict]:
    """Every parsable task; a broken file is reported on stderr and skipped rather than sinking the whole status."""
    metas = []
    for path in repo.task_files():
        try:
            meta, _ = parse_task(path)
        except FlowError as error:
            warn(f"skipping {error}")
            continue
        metas.append(meta)
    return metas


def status_rows(repo: Repo, brief: bool, cache: ForgeCache, forge: Forge | None) -> list[dict]:
    """The row model behind every renderer; `forge=None` reads the cache only (offline, and the hook)."""
    metas = load_tasks(repo)
    if forge is not None:
        try:
            sync(repo, metas, cache, forge, prune=True)
        except FlowError as error:
            age = cache.age_seconds()
            warn(f"{error}; MR facts from cache ({human_age(age)} old)" if age is not None
                 else f"{error}; no cache yet, MR states shown as `!N?`")
    current = repo.current_key()
    derived = [(meta, derive(meta, cache, repo.approvals_required)) for meta in metas]
    shown = [(meta, d) for meta, d in derived if not (brief and d.stage in INACTIVE and d.ball != "me")]
    repo.refs, repo.base_branch  # fill the process caches before the threads read them
    rows = parallel([lambda m=m, d=d: status_row(repo, m, d, cache, current) for m, d in shown])
    rows.sort(key=lambda r: (STAGES.index(r["stage"]), r["key"]))
    return rows


def status_row(repo: Repo, meta: dict, d: Derived, cache: ForgeCache, current: str | None) -> dict:
    """One task's row. Its keys are the `--json` contract; `board` and the tests read them by name."""
    mr = mr_facts(d.iid, d.entry, asked=cache.synced, prefix=repo.words.prefix)
    ab = branch_ab(repo, meta["branch"]) if meta["branch"] else ""
    journal = read_events(events_path(repo, meta["key"]))  # a file read: git is journaled by sync, log and clean
    return {
        "current": meta["key"] == current,
        "key": meta["key"], "stage": d.stage, "ball": d.ball,
        "stage_source": d.stage_source, "ball_source": d.ball_source,
        "branch": f"{meta['branch']} {ab}".strip() if meta["branch"] else "-",
        "branch_name": meta["branch"] or "", "branch_ab": ab,
        "wt": worktree_label(repo, meta),
        "mr": mr.label, "mr_iid": mr.iid, "mr_state": mr.state, "mr_draft": mr.draft, "mr_url": mr.url,
        "mr_mine": bool(d.entry and d.entry["author"] == cache.data.get("me")),
        "next": meta["next"] or "-",
        "updated": age_days(meta["updated"]), "updated_on": str(meta["updated"]),
        "age": age_int(meta["updated"]),
        "blocked": ", ".join(meta["blocked_on"]), "blocked_list": list(meta["blocked_on"]),
        "title": meta["title"],
        "last_event": journal[-1] if journal else None,
    }


# Row key -> (terminal/TSV header, markdown header). Other tools read `status --json` by field name; `--tsv` is
# for shell pipelines that cut by position, so extend TSV_COLUMNS only at the end.
COLUMNS = {"key": ("key", "Key"), "title": ("title", "Title"), "stage": ("stage", "Stage"), "ball": ("ball", "Ball"),
           "mr": ("mr", "MR"), "branch": ("branch", "Branch"), "wt": ("wt", "wt"), "next": ("next", "Next"),
           "updated": ("updated", "Updated"), "blocked": ("blocked", "Blocked on")}
TABLE_COLUMNS = ["key", "stage", "ball", "branch", "wt", "mr", "updated", "next"]
MD_COLUMNS = ["key", "title", "stage", "ball", "mr", "branch", "next", "updated"]
TSV_COLUMNS = ["key", "stage", "ball", "mr", "branch", "wt", "next", "updated", "blocked"]


def render_table(rows: list[dict]) -> str:
    if not rows:
        return "no tasks"
    cells = [{**r, "cur": "*" if r["current"] else " ", "next": truncate(str(r["next"]), NEXT_WIDTH)} for r in rows]
    cols = ["cur", *TABLE_COLUMNS]
    header = {"cur": "", **{c: COLUMNS[c][0] for c in TABLE_COLUMNS}}
    widths = {c: max(len(header[c]), *(len(str(r[c])) for r in cells)) for c in cols}
    lines = [" ".join(header[c].ljust(widths[c]) for c in cols).rstrip()]
    lines += [" ".join(str(r[c]).ljust(widths[c]) for c in cols).rstrip() for r in cells]
    return "\n".join(lines)


def truncate(value: str, width: int) -> str:
    return value if len(value) <= width else value[:width - 3] + "..."


def render_md(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS[c][1] for c in MD_COLUMNS) + " |", "|" + "---|" * len(MD_COLUMNS)]
    lines += ["| " + " | ".join(str(r[c]).replace("|", "\\|") for c in MD_COLUMNS) + " |" for r in rows]
    return "\n".join(lines)


def render_tsv(rows: list[dict]) -> str:
    return "\n".join("\t".join(str(r[c]) for c in TSV_COLUMNS) for r in rows)


def render_json(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2)


PLAN_SNAPSHOT_BEGIN, PLAN_SNAPSHOT_END = "<!-- plan:snapshot:begin -->", "<!-- plan:snapshot:end -->"


def hook_context(repo: Repo, rows: list[dict], cache: ForgeCache) -> str:
    table = render_table(rows) + hook_extras(repo, rows) + render_overlaps(repo, rows) + cache_line(repo, cache)
    return (
        f"flow status ({repo.primary.name}, * = current branch). Task files: {repo.tasks_dir}\n{table}\n"
        "flow commands — agent: `flow note \"...\" [KEY] --next \"...\"`, `flow next [KEY]`, "
        "`flow set <field> <value> [KEY]` (`flow set ball them --why ...` pins the ball), "
        "`flow status [--brief]`, `flow log [KEY]`, `flow sync`, `flow doctor`; "
        "human-only (own terminal): `flow start KEY slug --title ...`, `flow clean KEY`, "
        f"`flow set stage parked --blocked-on ...`, `flow migrate`. Full guide: `flow guide`. "
        "Update `next:` before ending the session."
    )


def write_snapshot(path: Path, md: str) -> None:
    """Replace the block between the snapshot markers of a hand-written Markdown file with the task table."""
    if not path.exists():
        raise FlowError(f"{path} does not exist; `--write PATH` updates a file that has the snapshot markers")
    text = path.read_text(encoding="utf-8")
    if PLAN_SNAPSHOT_BEGIN not in text or PLAN_SNAPSHOT_END not in text:
        raise FlowError(f"{path} has no {PLAN_SNAPSHOT_BEGIN} / {PLAN_SNAPSHOT_END} markers")
    head, rest = text.split(PLAN_SNAPSHOT_BEGIN, 1)
    _, tail = rest.split(PLAN_SNAPSHOT_END, 1)
    path.write_text(f"{head}{PLAN_SNAPSHOT_BEGIN}\n{md}\n{PLAN_SNAPSHOT_END}{tail}", encoding="utf-8")


def cmd_status(repo: Repo, args: argparse.Namespace) -> int:
    cache = ForgeCache(repo)
    forge = None if (args.hook or args.offline) else make_forge(repo)
    rows = status_rows(repo, brief=args.brief or args.hook, cache=cache, forge=forge)
    if args.hook:
        if rows:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": hook_context(repo, rows, cache)}}))
    elif args.md:
        md = render_md(rows)
        if args.write is None:
            print(md)
        else:
            path = Path(args.write).expanduser() if args.write else repo.local_docs / "PLAN.local.md"
            write_snapshot(path, md)
            print(f"snapshot regenerated in {path} ({len(rows)} rows)")
    elif args.tsv:
        print(render_tsv(rows))
    elif args.json:
        print(render_json(rows))
    elif args.html:
        write_html(repo, rows, args.out, args.open)
    else:
        print(render_table(rows) + render_overlaps(repo, rows))
    return 0


def write_html(repo: Repo, rows: list[dict], out: str | None, open_browser: bool) -> Path:
    import webbrowser

    from . import board  # presentation only; loaded on demand so plain `flow status` never imports it

    path = Path(out).expanduser() if out else repo.local_docs / "flow-status.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(board.render_html(rows, repo.primary.name, str(repo.config.get("jira_base") or "")),
                    encoding="utf-8")
    print(path)
    if open_browser:
        webbrowser.open(path.as_uri())
    return path


def cmd_board(repo: Repo, args: argparse.Namespace) -> int:
    """`flow board` = `flow status --html --open`: regenerate (a stale board lies) and open."""
    rows = status_rows(repo, brief=False, cache=ForgeCache(repo), forge=None if args.offline else make_forge(repo))
    write_html(repo, rows, out=None, open_browser=True)
    return 0


# --------------------------------------------------------------------------- set / note


def parse_iid(value: str) -> int:
    try:
        iid = int(value.lstrip("!"))
    except ValueError as error:
        raise FlowError(f"mr must be an iid like 42 or !42, got {value!r}") from error
    if iid < 1:
        raise FlowError(f"mr iid must be positive, got {iid}")
    return iid


def cmd_set(repo: Repo, args: argparse.Namespace) -> int:
    key = pick_key(repo, args.key, "set")
    field, value = args.field, args.value
    if field not in FIELDS or field in ("key", "ball_pin", "updated"):
        raise FlowError(f"cannot set {field!r}; fields: "
                        + ", ".join(f for f in FIELDS if f not in ("key", "ball_pin", "updated")))
    path, meta, body = load_task(repo, key)
    if field == "stage":
        if value not in STAGES:
            raise FlowError(f"unknown stage {value!r}; stages: {', '.join(STAGES)}")
        if value == "cleaned":
            raise FlowError("`cleaned` is set by `flow clean` only", EXIT_PRECONDITION)
        if value in FORGE_STAGES:
            raise FlowError(f"{value!r} comes from the MR, not the task file: `flow sync` refreshes it",
                            EXIT_PRECONDITION)
        if value == "parked":
            require_human("set stage parked")
            if not (meta["blocked_on"] or args.blocked_on):
                raise FlowError("parking needs a reason: add --blocked-on <KEY|person>", EXIT_PRECONDITION)
        meta["stage"] = value
    elif field == "ball":
        set_ball_pin(repo, meta, value, args.why)
    elif field in ("blocked_on", "docs"):
        meta[field] = split_csv(value)
    elif field == "mr":
        iid = None if value in ("", "null", "none", "-") else parse_iid(value)
        if iid is not None and meta["branch"]:
            raise FlowError(f"{key} has branch {meta['branch']}: its MR is found by branch (`flow sync`); "
                            "`mr` is stored only for tasks without a branch", EXIT_PRECONDITION)
        meta["mr"] = iid
    elif field in ("title", "next"):
        meta[field] = single_line(value, field)
    else:
        meta[field] = value
    if args.blocked_on:
        meta["blocked_on"] = split_csv(args.blocked_on)
    save_task(path, meta, body)
    shown = meta["ball_pin"] if field == "ball" else meta[field]
    print(f"{key}: {field} = {shown!r}")
    return 0


def set_ball_pin(repo: Repo, meta: dict, value: str, why: str | None) -> None:
    """The ball is derived; a pin overrides it for as long as the MR stays as it was when the pin was set."""
    meta["ball"] = None
    if value == "auto":
        meta["ball_pin"] = None
        return
    if value not in BALLS:
        raise FlowError("ball must be me|them|— (or `auto` to drop the pin)")
    if not why:
        raise FlowError("the ball is derived from the MR; pinning it needs a reason: --why \"...\"")
    cache = ForgeCache(repo)
    iid = cache.iid_for(meta)
    if iid is not None and cache.mr(iid) is None:
        raise FlowError(f"no cached facts for !{iid}: `flow sync` first, so the pin knows what state it pins",
                        EXIT_PRECONDITION)
    meta["ball_pin"] = {"value": value, "why": single_line(why, "why"), "since": today(),
                        "fingerprint": fingerprint(cache.mr(iid))}


def changed_files(repo: Repo, since: str, until: str) -> set[str]:
    out = git("diff", "--name-only", f"{since}..{until}", cwd=repo.primary, check=False)
    return set(out.splitlines()) if out else set()


def name_files(files: set[str]) -> str:
    shown = sorted(files)[:OVERLAP_FILES_SHOWN]
    rest = len(files) - len(shown)
    return ", ".join(shown) + (f" +{rest} more" if rest else "")


def overlap_lines(repo: Repo, rows: list[dict]) -> list[str]:
    """Work that collides before a rebase or a merge says so: base drift under a branch, and branches sharing files.

    Committed changes only, measured from each branch's merge base with `origin/<base>` as last fetched -- so it
    stays offline and fits the SessionStart hook. A branch built on another task's branch shares all of its files by
    construction; that pair is reported once as stacked rather than as an overlap.
    """
    base = repo.base_ref
    live = [r for r in rows if r["stage"] not in INACTIVE and r["branch_name"] and branch_exists(repo, r["branch_name"])]
    own: dict[str, set[str]] = {}
    base_moved = functools.lru_cache(maxsize=None)(lambda fork: changed_files(repo, fork, base))
    lines = []
    for row in live:
        branch = row["branch_name"]
        fork = git("merge-base", branch, base, cwd=repo.primary, check=False)
        if not fork:
            continue
        own[row["key"]] = {f for f in changed_files(repo, fork, branch) if not repo.overlap_ignored(f)}
        drift = own[row["key"]] & base_moved(fork)  # branches forked from one commit share the base's drift
        if drift:
            log = git("log", "--format=%h %s", f"{fork}..{base}", "--", *sorted(drift), cwd=repo.primary,
                      check=False).splitlines()
            latest = truncate(log[0], 60) if log else "?"
            more = f" (+{len(log) - 1} more)" if len(log) > 1 else ""
            lines.append(f"{row['key']} <- {repo.base_branch} moved under it: {latest}{more}; shared files:"
                         f" {name_files(drift)}")
    keyed = [r for r in live if r["key"] in own]
    for i, first in enumerate(keyed):
        for second in keyed[i + 1:]:
            shared = own[first["key"]] & own[second["key"]]
            if not shared:
                continue  # a stacked pair always shares the lower branch's files, so no git call is needed here
            a, b = first["branch_name"], second["branch_name"]
            # Commits the two share beyond the base: one is built on the other, whether or not the lower branch has
            # moved on since. Their file overlap is that shared history, not a collision.
            shared_tip = git("merge-base", a, b, cwd=repo.primary, check=False)
            if shared_tip and not is_ancestor(repo, shared_tip, base):
                lines.append(stack_relation(repo, first, second, shared_tip, base))
                continue
            lines.append(f"{first['key']} <-> {second['key']}: both change {name_files(shared)}")
    return lines


def stack_relation(repo: Repo, first: dict, second: dict, shared_tip: str, base: str) -> str:
    if git("rev-parse", first["branch_name"], cwd=repo.primary) == shared_tip:
        return f"{second['key']} is stacked on {first['key']}"
    if git("rev-parse", second["branch_name"], cwd=repo.primary) == shared_tip:
        return f"{first['key']} is stacked on {second['key']}"
    count = git("rev-list", "--count", f"{base}..{shared_tip}", cwd=repo.primary, check=False)
    return (f"{first['key']} and {second['key']} share {count} unmerged commit(s): one is built on the other, "
            "and the lower one has moved on since")


def render_overlaps(repo: Repo, rows: list[dict]) -> str:
    lines = overlap_lines(repo, rows)
    return ("\noverlaps:" + "".join(f"\n  {line}" for line in lines)) if lines else ""


def is_ancestor(repo: Repo, older: str, newer: str) -> bool:
    return git_ok("merge-base", "--is-ancestor", older, newer, cwd=repo.primary)


def hook_extras(repo: Repo, rows: list[dict]) -> str:
    """What the table truncates or leaves implicit: the current task's full `next`, and commands only a human runs."""
    extras = []
    for row in rows:
        if row["current"] and len(str(row["next"])) > NEXT_WIDTH:
            extras.append(f"* {row['key']} next (full): {row['next']}")
    human = [f"flow clean {row['key']}" for row in rows if row["stage"] == "merged"]
    w = repo.words
    human += [f"un-draft {w.prefix}{row['mr_iid']} ({row['key']}) in {w.product}" for row in rows
              if row["mr_draft"] and row["mr_mine"] and row["mr_state"] == "opened"]
    if human:
        extras.append("for the human (own terminal): " + "; ".join(human))
    return "".join(f"\n{line}" for line in extras)


def cache_line(repo: Repo, cache: ForgeCache) -> str:
    """Stage and ball are only as fresh as the cache; say how fresh, and refresh it in the background if stale."""
    started = start_background_sync(repo, cache)
    age = cache.age_seconds()
    if age is None:
        return "\nforge: no cache yet -- stages from task files" + ("; background sync started" if started else "")
    if age < CACHE_STALE_SECONDS:
        return ""
    return f"\nforge: cache {human_age(age)} old" + ("; background sync started" if started else "")


def cmd_note(repo: Repo, args: argparse.Namespace) -> int:
    key = pick_key(repo, args.key, "note")
    path, meta, body = load_task(repo, key)
    text = LEADING_DATE.sub("", args.text, count=1) or args.text
    body = (body.rstrip("\n") + f"\n- {today()}: {text}\n").lstrip("\n")
    if args.next:
        meta["next"] = single_line(args.next, "next")
    save_task(path, meta, body)
    print(f"{key}: noted" + (f"; next = {args.next!r}" if args.next else ""))
    return 0


# --------------------------------------------------------------------------- next


def merge_base(repo: Repo, cwd: Path) -> str:
    return git("merge-base", repo.base_ref, "HEAD", cwd=cwd)


def run_gate(repo: Repo, cwd: Path) -> None:
    gate = repo.gate()
    if not gate:
        raise FlowError("no gate configured (local-docs/flow.local.yml `gate:`); pass --no-gate --why \"...\" to skip",
                        EXIT_PRECONDITION)
    command = expand(gate, "gate", base=merge_base(repo, cwd))
    print(f"gate: {command}  (cwd {cwd})")
    result = subprocess.run(command, shell=True, cwd=cwd)
    if result.returncode != 0:
        raise FlowError(f"gate failed (exit {result.returncode}); stage unchanged", EXIT_PRECONDITION)


def require_pushed(repo: Repo, cwd: Path, branch: str) -> None:
    """Asks origin itself: a tracking ref survives a server-side delete until a pruning fetch."""
    git("fetch", "--quiet", "origin", cwd=cwd, check=False)
    listed = run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd)
    if listed.returncode != 0:
        raise FlowError(f"cannot reach origin: {listed.stderr.strip()}", EXIT_PRECONDITION)
    if not listed.stdout.strip():
        raise FlowError(f"{branch} is not on origin: git push -u origin {branch}", EXIT_PRECONDITION)
    remote_tip, local_tip = listed.stdout.split()[0], git("rev-parse", branch, cwd=cwd)
    if remote_tip != local_tip:
        if not is_ancestor(repo, remote_tip, local_tip):
            raise FlowError(f"{branch} and origin/{branch} have diverged: pull/rebase, or force-push deliberately",
                            EXIT_PRECONDITION)
        ahead = git("rev-list", "--count", f"{remote_tip}..{local_tip}", cwd=cwd)
        raise FlowError(f"{branch} is {ahead} commit(s) ahead of origin: push first", EXIT_PRECONDITION)
    if is_dirty(cwd):
        raise FlowError("worktree has uncommitted changes; commit or stash first", EXIT_PRECONDITION)


IN_PROGRESS = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
               ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))


def require_settled(cwd: Path) -> None:
    """A half-done merge or rebase would reach the gate and the MR as if it were the branch's content."""
    for name, what in IN_PROGRESS:
        path = Path(git("rev-parse", "--git-path", name, cwd=cwd))
        if (path if path.is_absolute() else cwd / path).exists():
            raise FlowError(f"{what} in progress in {cwd}; finish or abort it first", EXIT_PRECONDITION)


def warn_base_conflicts(repo: Repo, cwd: Path) -> None:
    """Conflicts with the base do not block review -- both forges review a conflicted MR -- but say so up front."""
    base = repo.base_ref
    result = run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", base, "HEAD"], cwd)
    if result.returncode == 1:
        files = [line for line in result.stdout.splitlines()[1:] if line]
        warn(f"warning: HEAD conflicts with {base} in {name_files(set(files))}; "
             "the MR will show conflicts until you rebase")


def to_review(repo: Repo, meta: dict, no_gate: bool, forge: Forge, cache: ForgeCache) -> None:
    """Gate, push, and an MR: the cache was synced for this task just before, so it already knows an open one."""
    branch = meta["branch"] or git("branch", "--show-current", cwd=repo.resolve_worktree(meta), check=False)
    if not branch:
        raise FlowError("task has no branch; set it with `flow set branch <name>`", EXIT_PRECONDITION)
    meta["branch"] = branch
    cwd = repo.branch_worktree(branch)
    if cwd is None:
        raise FlowError(f"{branch} is not checked out in any worktree (detached HEAD, or a rebase in progress?)",
                        EXIT_PRECONDITION)
    require_settled(cwd)
    warn_base_conflicts(repo, cwd)
    if not no_gate:
        run_gate(repo, cwd)
    require_pushed(repo, cwd, branch)
    existing = cache.mr(cache.data["by_branch"].get(branch))
    if existing and existing["state"] == "opened":
        print(f"found open {repo.words.noun} {repo.words.prefix}{cache.data['by_branch'][branch]}")
    else:
        title = f"{meta['key']}: {meta['title'] or branch}"
        w = repo.words
        print(f"created Draft {w.noun} {w.prefix}{forge.mr_create(branch, repo.base_branch, title)}: {title}")


def cmd_next(repo: Repo, args: argparse.Namespace) -> int:
    """Advance what the task file owns; for everything after the MR, act and then report what the forge says."""
    if args.no_gate != bool(args.why):
        raise FlowError("--no-gate needs --why \"...\" (and --why needs --no-gate): a skipped gate leaves its reason "
                        "in the task notes", EXIT_PRECONDITION)
    why = single_line(args.why, "why") if args.why else None
    key = pick_key(repo, args.key, "next")
    path, meta, body = load_task(repo, key)
    stored = meta["stage"]
    if stored == "cleaned":
        raise FlowError("task is cleaned; nothing further", EXIT_PRECONDITION)
    if stored == "parked":
        raise FlowError("task is parked; resume with `flow set stage <stage>`", EXIT_PRECONDITION)
    cache = ForgeCache(repo)
    if stored in ("plan", "refine", "implement") and cache.iid_for(meta) is None:
        meta["stage"] = WORK_STAGES[WORK_STAGES.index(stored) + 1]
        save_task(path, meta, body)
        print(f"{key}: {stored} → {meta['stage']}")
        return 0
    forge = make_forge(repo)
    sync(repo, [meta], cache, forge, prune=False)
    before = derive(meta, cache, repo.approvals_required)
    if before.stage == "merged":
        raise FlowError("merged → cleaned goes through `flow clean`", EXIT_PRECONDITION)
    if before.closed:
        w = repo.words
        raise FlowError(f"{w.prefix}{before.iid} is closed; open a new {w.noun} (`flow next` from test) or park",
                        EXIT_PRECONDITION)
    if before.stage in ("test", "changes"):
        to_review(repo, meta, args.no_gate, forge, cache)
        if why:  # only once the review really went out: a skip that stopped short of it skipped nothing
            body = (body.rstrip("\n") + f"\n- {today()}: gate skipped (--no-gate): {why}\n").lstrip("\n")
        sync(repo, [meta], cache, forge, prune=False)
    if meta["stage"] in FORGE_STAGES:
        meta["stage"] = "test"  # a legacy file stored a forge stage; the last stage it owns is test
    save_task(path, meta, body)
    after = derive(meta, cache, repo.approvals_required)
    moved = "" if after.stage != before.stage else " (nothing to advance until the MR moves)"
    print(f"{key}: {before.stage} → {after.stage} (ball={after.ball}, from !{after.iid}){moved}")
    return 0


def cmd_sync(repo: Repo, args: argparse.Namespace) -> int:
    """Refresh the forge cache, then journal every task from git and the fresh forge facts."""
    cache = ForgeCache(repo)
    try:
        tasks = load_tasks(repo)
        sync(repo, tasks, cache, make_forge(repo), prune=True)
        for meta in tasks:
            collect_events(repo, meta, cache)
    finally:
        (cache.path.parent / "sync.lock").unlink(missing_ok=True)
    if not args.quiet:
        print(f"synced {len(cache.data['mrs'])} MR(s) for {cache.data['me']} -> {cache.path}")
    return 0


# --------------------------------------------------------------------------- start / clean


def link_local_docs(repo: Repo, worktree: Path) -> None:
    """Symlink the primary local-docs into a worktree and hide the link from git.

    `.gitignore` rules like `local-docs/` match directories only, so the symlink itself would show
    up as untracked and `git add -A` would stage it. The per-worktree info/exclude closes that.
    """
    link = worktree / "local-docs"
    if not (link.is_symlink() or link.exists()):
        link.symlink_to(repo.local_docs, target_is_directory=True)
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=worktree))
    if not exclude.is_absolute():
        exclude = worktree / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if not re.search(r"^/?local-docs$", existing, re.M):
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(("" if existing.endswith("\n") or not existing else "\n") + "/local-docs\n")


def cmd_start(repo: Repo, args: argparse.Namespace) -> int:
    require_human("start")
    key, slug = args.key, args.slug
    if not KEY_RE.match(key):
        raise FlowError(f"{key!r} is not a task key (expected e.g. PL-1234)")
    if not SLUG_RE.match(slug):
        raise FlowError(f"{slug!r} is not a kebab-case slug")
    single_line(args.title, "title")
    branch = f"{key}-{slug}"
    target = Path(args.dir).resolve() if args.dir else repo.worktree_dir(key)
    if target.exists():
        raise FlowError(f"{target} already exists")
    if branch_exists(repo, branch):
        raise FlowError(f"branch {branch} already exists")
    print(f"fetching origin ...")
    git("fetch", "--quiet", "origin", cwd=repo.primary)
    git("worktree", "add", str(target), "-b", branch, repo.base_ref, cwd=repo.primary)
    repo.forget_git_state()
    link_local_docs(repo, target)
    path = repo.task_path(key)
    if path.exists():
        meta, body = parse_task(path)
    else:
        meta, body = default_meta(key), ""
        meta["next"] = "refine: read the task, fill notes and next"
    meta["branch"] = branch
    meta["worktree"] = os.path.relpath(target, repo.primary)
    if args.title:
        meta["title"] = args.title
    if meta["stage"] in INACTIVE:
        meta["stage"] = "plan"
    save_task(path, meta, body)
    print(f"worktree {target}\nbranch   {branch}\ntask     {path}\n\ncd {target} && claude")
    return 0


def commits_only_here(repo: Repo, branch: str, mr_sha: str | None) -> list[str]:
    """Commits `branch -D` would destroy: reachable from no remote-tracking ref and not from the MR's head.

    The MR head counts because the forge may delete the source branch on merge and may squash, so after a merge the
    branch's commits can legitimately be on no remote at all.
    """
    git("fetch", "--quiet", "origin", cwd=repo.primary, check=False)
    exclude = ["--remotes"]
    if mr_sha and git_ok("cat-file", "-e", f"{mr_sha}^{{commit}}", cwd=repo.primary):
        exclude.append(mr_sha)  # an MR head this clone never saw proves nothing about local commits
    out = git("log", "--format=%h %s", branch, "--not", *exclude, cwd=repo.primary, check=False)
    return out.splitlines() if out else []


def prove_merged(repo: Repo, meta: dict) -> str | None:
    """The merged MR's head sha; refuses when the forge cannot show the task's MR as merged."""
    forge = make_forge(repo)
    if meta.get("mr"):
        data = forge.mr_view(int(meta["mr"]))
    else:
        data = forge.latest_mr_for_branch(meta["branch"]) if meta["branch"] else None
    if not data:
        raise FlowError("no MR found for the task — cannot prove it merged; use --force-unmerged", EXIT_PRECONDITION)
    if data.get("state") != "merged":
        raise FlowError(f"{repo.words.prefix}{data.get('iid')} is {data.get('state')}, not merged; refuse to clean "
                        "(--force-unmerged to override)", EXIT_PRECONDITION)
    return data.get("sha")


def refuse_losing_commits(repo: Repo, branch: str, mr_sha: str | None) -> None:
    lost = commits_only_here(repo, branch, mr_sha)
    if lost:
        raise FlowError(f"{branch} has {len(lost)} commit(s) that are on no remote and not in the merged MR:\n  "
                        + "\n  ".join(lost[:5]) + ("\n  ..." if len(lost) > 5 else "")
                        + "\npush them to a new branch, or --discard-commits", EXIT_PRECONDITION)


def archive_task(repo: Repo, key: str, path: Path, meta: dict, body: str) -> Path:
    """Move the task file and its journal to `done/`, recording that git no longer has its worktree or branch."""
    meta["stage"] = "cleaned"
    meta["ball"] = meta["ball_pin"] = None
    meta["worktree"] = ""
    done = repo.task_path(key, done=True)
    save_task(done, meta, body.rstrip("\n") + f"\n- {today()}: cleaned (worktree and branch removed)\n")
    path.unlink()
    journal = events_path(repo, key)
    if journal.exists():
        os.replace(journal, events_path(repo, key, done=True))
    return done


def pull_base(repo: Repo, key: str) -> None:
    """Convenience only: after a clean the primary usually wants the merged base. Never fails the clean."""
    current = git("branch", "--show-current", cwd=repo.primary, check=False)
    if current != repo.base_branch:
        git("fetch", "--quiet", "origin", cwd=repo.primary, check=False)
        print(f"primary is on {current!r}, not {repo.base_branch}: fetched only")
        return
    try:
        git("pull", "--ff-only", "origin", repo.base_branch, cwd=repo.primary)
        print(f"pulled {repo.base_branch}")
    except FlowError as error:
        warn(f"{key} is cleaned, but the pull did not happen — {error}\n"
             f"       reconcile {repo.primary} by hand: git pull --ff-only origin {repo.base_branch}")


def cmd_clean(repo: Repo, args: argparse.Namespace) -> int:
    require_human("clean")
    key = pick_key(repo, args.key, "clean")
    path, meta, body = load_task(repo, key)
    worktree = repo.resolve_worktree(meta) if meta.get("worktree") else None
    if worktree and worktree == repo.root:
        raise FlowError("run `flow clean` from the primary checkout, not from the worktree being removed")
    mr_sha = None if args.force_unmerged else prove_merged(repo, meta)
    branch = meta.get("branch")
    if branch and branch_exists(repo, branch) and not args.discard_commits:
        refuse_losing_commits(repo, branch, mr_sha)
    collect_events(repo, meta, ForgeCache(repo))  # before `branch -D` takes the reflog with it
    if worktree and worktree.exists():
        if is_dirty(worktree):
            raise FlowError(f"{worktree} has uncommitted changes; commit, stash, or discard first",
                            EXIT_PRECONDITION)
        git("worktree", "remove", str(worktree), cwd=repo.primary)
        print(f"removed worktree {worktree}")
    if branch and branch_exists(repo, branch):
        git("branch", "-D", branch, cwd=repo.primary)
        print(f"deleted branch {branch}")
    git("worktree", "prune", cwd=repo.primary, check=False)
    repo.forget_git_state()
    # Archive before the convenience pull: git is already mutated, so the task file must agree even if it fails.
    print(f"{key}: cleaned → {archive_task(repo, key, path, meta, body)}")
    pull_base(repo, key)
    return 0


# --------------------------------------------------------------------------- doctor


def consistency_problems(meta: dict, d: Derived, cache: ForgeCache, w: ForgeWords) -> list[str]:
    """What the derivation cannot settle on its own, so a human or the agent has to."""
    problems = []
    if meta["stage"] in FORGE_STAGES or meta.get("ball") is not None:
        problems.append("legacy stored stage/ball -- human: `flow migrate --dry-run`, then `--apply`")
    if d.pin_stale:
        pin = meta["ball_pin"]
        problems.append(f"ball pinned to {pin.get('value')} since {pin.get('since')} ({pin.get('why')}), "
                        "but the MR moved since -- re-pin or `flow set ball auto`")
    if d.closed:
        problems.append(f"{w.prefix}{d.iid} is closed -- open a new {w.noun} (`flow next` from test) or park")
    branch_iid = cache.data["by_branch"].get(meta["branch"]) if meta["branch"] else None
    if meta.get("mr") and meta["branch"]:
        problems.append(f"stores mr {w.prefix}{meta['mr']} although it has branch {meta['branch']}"
                        + (f" (whose {w.noun} is {w.prefix}{branch_iid})" if branch_iid and branch_iid != meta["mr"]
                           else "")
                        + " -- `flow migrate`")
    if d.stage == "merged":
        merged_on = (d.entry or {}).get("merged_at") or str(meta["updated"])
        age = age_int(merged_on)
        if age is not None and age >= MERGED_STALE_DAYS:
            problems.append(f"merged {age}d ago and not cleaned -- human: `flow clean {meta['key']}`")
    return problems


def cmd_doctor(repo: Repo, args: argparse.Namespace) -> int:
    problems = 0
    if not repo.local_docs.is_dir():
        problems += 1
        print(f"{repo.local_docs} is missing: every task file and every worktree link points at it")
    tasks = {meta["key"]: meta for meta in load_tasks(repo)}
    cache = ForgeCache(repo)
    if not args.offline:
        try:
            forge = make_forge(repo)
            sync(repo, list(tasks.values()), cache, forge, prune=True)
            print(f"{forge.cli}: ok")
        except FlowError as error:
            print(f"{error} -- checks below use the cache")
    derived = {key: derive(meta, cache, repo.approvals_required) for key, meta in tasks.items()}
    for path, branch in repo.worktrees.items():
        if path == repo.primary:
            continue
        link = path / "local-docs"
        if link.exists() and not link.is_symlink():
            problems += 1
            print(f"worktree {path}: local-docs is a real directory, not a link to {repo.local_docs}; move it by hand")
        elif not link.is_symlink() or link.resolve() != repo.local_docs.resolve() or not link.exists():
            problems += 1
            what = "dangling local-docs symlink" if link.is_symlink() else "no local-docs symlink"
            print(f"worktree {path}: {what} (flow doctor --fix)")
            if args.fix:
                if link.is_symlink():
                    link.unlink()
                link_local_docs(repo, path)
                print("  linked")
        key = KEY_IN_BRANCH.match(branch or "")
        if key and key.group(1) not in tasks:
            problems += 1
            print(f"worktree {path} on {branch}: no task file {repo.task_path(key.group(1))}")
    for line in git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=repo.primary).splitlines():
        key = KEY_IN_BRANCH.match(line)
        if key and key.group(1) not in tasks:
            done = repo.task_path(key.group(1), done=True)
            if not done.exists():
                problems += 1
                print(f"branch {line}: no task file")
    for key, meta in tasks.items():
        active = derived[key].stage not in INACTIVE
        if meta["branch"] and active and branch_ab(repo, meta["branch"]) == "gone":
            problems += 1
            print(f"{key}: branch {meta['branch']} missing locally")
        if meta.get("worktree") and not repo.resolve_worktree(meta).exists():
            problems += 1
            print(f"{key}: worktree {meta['worktree']} missing")
        held = repo.branch_worktree(meta["branch"]) if meta["branch"] and active else None
        if held and held != repo.resolve_worktree(meta):
            problems += 1
            rel = os.path.relpath(held, repo.primary) if held != repo.primary else "''"
            print(f"{key}: {meta['branch']} is checked out in {held}, task says {meta['worktree'] or 'primary'} "
                  f"(flow set worktree {rel} {key})")
        for problem in consistency_problems(meta, derived[key], cache, repo.words):
            problems += 1
            print(f"{key}: {problem}")
    print(f"{problems} problem(s)" if problems else "ok")
    return 1 if problems else 0


# --------------------------------------------------------------------------- migrate


def migrate_meta(meta: dict, cache: ForgeCache, approvals_required: int) -> tuple[dict, str | None]:
    """The task file with every derivable field removed, plus advice when a stored ball disagrees with the MR.

    A disagreeing ball is pinned only where the forge has no evidence (no MR): there the stored value is the only
    knowledge there is. Where an MR exists, the stored ball is what drifted, so it is dropped and the human is told
    how to re-pin it deliberately -- pinning it wholesale would preserve exactly the drift migration removes.
    """
    new = dict(meta)
    if new["stage"] in FORGE_STAGES:
        new["stage"] = "test"
    if new.get("mr") and new["branch"]:
        new["mr"] = None  # the branch finds it; a different stored iid was reported by doctor
    new["ball"] = None
    d = derive({**new, "ball_pin": None}, cache, approvals_required)
    stored = meta.get("ball")
    if stored is None or stored == d.ball or new["stage"] == "cleaned" or new.get("ball_pin"):
        return new, None
    if d.entry is None:
        new["ball_pin"] = {"value": stored, "why": f"migrated {today()}: kept from the task file, no MR to derive from",
                           "since": today(), "fingerprint": d.fingerprint}
        return new, None
    return new, (f"ball: stored {stored}, !{d.iid} says {d.ball} -- to keep yours: "
                 f"flow set ball {stored} {meta['key']} --why \"...\"")


def cmd_migrate(repo: Repo, args: argparse.Namespace) -> int:
    require_human("migrate")
    cache = ForgeCache(repo)
    if not cache.synced:
        raise FlowError("no forge cache: run `flow sync` first, so migration knows each task's MR", EXIT_PRECONDITION)
    changed = 0
    for path in repo.task_files():
        meta, body = parse_task(path)
        new, advice = migrate_meta(meta, cache, repo.approvals_required)
        diff = [f"{field}: {meta.get(field)!r} -> {new.get(field)!r}" for field in FIELDS
                if meta.get(field) != new.get(field)] + ([advice] if advice else [])
        if not diff:
            continue
        changed += 1
        print(f"{meta['key']}:\n  " + "\n  ".join(diff))
        if args.apply:
            dump_task(path, new, body)  # not save_task: migration is not an update of the task
    print(f"{changed} file(s) {'rewritten' if args.apply else 'would change; --apply to write'}")
    return 0



# --------------------------------------------------------------------------- cli


# --------------------------------------------------------------------------- setup (no repo needed)

HOOK_COMMAND = "flow status --hook"
HOOK_MATCHER = "startup|clear|compact"
# A hook entry is flow's own when its command runs `status --hook` through `flow` or a `flow.py` script path.
OWN_HOOK = re.compile(r"(?:^|[\s/])flow(?:\.py)? status --hook\b")


def cmd_guide(args: argparse.Namespace) -> int:
    """Print the user guide shipped inside the package."""
    from importlib.resources import files
    print(files(__package__).joinpath("guide.md").read_text(encoding="utf-8"), end="")
    return 0


def merge_hook(settings: dict) -> dict:
    """Return settings with flow's SessionStart hook present exactly once.

    An existing flow entry (any matcher, script-path or installed form) is replaced in place, so re-running is
    idempotent and migrates a `python3 .../flow.py status --hook` entry to the installed command.
    """
    hooks = settings.setdefault("hooks", {})
    groups = hooks.setdefault("SessionStart", [])
    wanted = {"type": "command", "command": HOOK_COMMAND}
    found = False
    for group in groups:
        kept = []
        for hook in group.get("hooks", []):
            if OWN_HOOK.search(str(hook.get("command", ""))):
                if found:
                    continue  # a duplicate flow entry: drop it
                found = True
                kept.append(wanted)
            else:
                kept.append(hook)
        group["hooks"] = kept
    groups[:] = [g for g in groups if g.get("hooks")]
    if not found:
        groups.append({"matcher": HOOK_MATCHER, "hooks": [wanted]})
    return settings


def cmd_install_hook(args: argparse.Namespace) -> int:
    """Put `flow status --hook` into a Claude Code settings file's SessionStart hooks (idempotent)."""
    if args.print:
        print(json.dumps(merge_hook({}), indent=2))
        return 0
    require_human("install-hook")  # editing an agent's own settings is the human's call
    path = Path(args.settings).expanduser()
    existed = path.exists()
    before = path.read_text(encoding="utf-8") if existed else ""
    try:
        settings = json.loads(before or "{}")
    except json.JSONDecodeError as error:
        raise FlowError(f"{path} is not valid JSON ({error}); fix it or use --print and merge by hand")
    if not isinstance(settings, dict):
        raise FlowError(f"{path} does not hold a JSON object; use --print and merge by hand")
    merged = merge_hook(json.loads(json.dumps(settings)))
    if merged == settings:
        print(f"{path}: flow hook already installed")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        path.with_name(path.name + ".bak").write_text(before, encoding="utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"{path}: SessionStart hook set to `{HOOK_COMMAND}`" + (f" (backup: {path.name}.bak)" if existed else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="table of tasks (git only with --offline/--hook)")
    p.add_argument("--brief", action="store_true", help="active tasks and ball=me only")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--md", action="store_true", help="markdown table (see --write)")
    fmt.add_argument("--tsv", action="store_true", help="columns: " + ", ".join(TSV_COLUMNS))
    fmt.add_argument("--json", action="store_true", help="full row model as JSON")
    fmt.add_argument("--html", action="store_true", help="kanban board; writes local-docs/flow-status.html")
    fmt.add_argument("--hook", action="store_true", help="SessionStart hook JSON; silent when no tasks")
    p.add_argument("--write", nargs="?", const="", metavar="PATH",
                   help="with --md: replace the block between the plan:snapshot markers of PATH "
                        "(default local-docs/PLAN.local.md)")
    p.add_argument("--out", help="with --html: output path")
    p.add_argument("--open", action="store_true", help="with --html: open in the browser")
    p.add_argument("--offline", action="store_true", help="skip the forge")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("board", help="regenerate the kanban board and open it (= status --html --open)")
    p.add_argument("--offline", action="store_true", help="skip the forge")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("start", help="[human] worktree + branch + task file")
    p.add_argument("key")
    p.add_argument("slug")
    p.add_argument("--title", default="")
    p.add_argument("--dir", help="worktree path (default from config)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("next", help="advance the stage; preconditions enforced")
    p.add_argument("key", nargs="?")
    p.add_argument("--no-gate", action="store_true", help="skip the gate on test→review-wait; needs --why")
    p.add_argument("--why", help="with --no-gate: why the gate cannot pass here; kept as a task note")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("set", help="set a frontmatter field")
    p.add_argument("field")
    p.add_argument("value")
    p.add_argument("key", nargs="?")
    p.add_argument("--blocked-on", dest="blocked_on", help="comma list, required with `stage parked`")
    p.add_argument("--why", help="with `ball`: why the derived ball is wrong (required to pin)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("note", help="append a dated note; optionally set next")
    p.add_argument("text")
    p.add_argument("key", nargs="?")
    p.add_argument("--next", help="also set the next action")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("clean", help="[human] remove worktree+branch after merge, archive task")
    p.add_argument("key", nargs="?")
    p.add_argument("--force-unmerged", action="store_true")
    p.add_argument("--discard-commits", action="store_true", help="delete commits that exist on no remote")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("doctor", help="consistency checks: symlinks, branches, task files, glab")
    p.add_argument("--fix", action="store_true", help="create missing local-docs symlinks")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("log", help="task journal: git reflog + forge events + hand-written notes, by time")
    p.add_argument("key", nargs="?")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("sync", help="ask the forge about every task's MR and refresh the forge cache")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("migrate", help="[human] drop stored stage/ball/mr that are now derived from the MR")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("guide", help="print the user guide")
    p.set_defaults(func=cmd_guide, needs_repo=False)

    p = sub.add_parser("install-hook", help="[human] add the SessionStart hook to Claude Code settings")
    p.add_argument("--settings", default="~/.claude/settings.json", metavar="PATH",
                   help="settings file to edit (default: %(default)s; a project uses .claude/settings.json)")
    p.add_argument("--print", action="store_true", help="print the JSON snippet instead of editing a file")
    p.set_defaults(func=cmd_install_hook, needs_repo=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not getattr(args, "needs_repo", True):
            return args.func(args)
        today()  # reject a malformed FLOW_TODAY once, up front, instead of per task file
        repo = Repo()
        return args.func(repo, args)
    except FlowError as error:
        warn(str(error))
        # A hook must not fail the session over a flow problem; the reason is on stderr for the log.
        return 0 if getattr(args, "hook", False) else error.code


if __name__ == "__main__":
    sys.exit(main())
