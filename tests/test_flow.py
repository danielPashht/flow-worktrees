"""Tests for flow — a throwaway git repo with a bare origin and a fake `glab` on PATH.

Run: uv run pytest
"""

from __future__ import annotations

import datetime
import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


FAKE_GLAB = r'''#!/usr/bin/env python3
import json, os, re, sys
state_path = os.environ["FAKE_GLAB_STATE"]
state = json.load(open(state_path))
if state.get("offline"):
    sys.stderr.write('Get "https://gitlab.example/api/v4/user": dial tcp 10.0.0.1:443: i/o timeout\n'); sys.exit(1)
args = sys.argv[1:]
def full(iid, mr):
    out = dict(mr, iid=int(iid))
    out.setdefault("author", {"username": "me"})
    return out
if args[0] == "api":
    path = args[1]
    with open(state_path + ".calls", "a") as log:  # append: sync runs glab calls in parallel
        log.write(path + "\n")
    if path == "projects/:id":
        print(json.dumps({"id": 1})); sys.exit(0)
    if path == "user":
        print(json.dumps({"username": state.get("me", "me")})); sys.exit(0)
    m = re.match(r"projects/:id/merge_requests/(\d+)/discussions", path)
    if m:
        print(json.dumps(state["mrs"].get(m.group(1), {}).get("discussions", []))); sys.exit(0)
    m = re.match(r"projects/:id/merge_requests/(\d+)/approvals$", path)
    if m:
        mr = state["mrs"].get(m.group(1), {})
        print(json.dumps({"approved_by": [{}] * mr.get("approvals", 0)})); sys.exit(0)
    m = re.match(r"projects/:id/merge_requests/(\d+)$", path)
    if m:
        mr = state["mrs"].get(m.group(1))
        if not mr:
            sys.stderr.write("404 Not Found\n"); sys.exit(1)
        print(json.dumps(full(m.group(1), mr))); sys.exit(0)
    m = re.match(r"projects/:id/merge_requests\?((?:iids\[\]=\d+&?)+)&per_page=\d+$", path)
    if m:
        wanted = re.findall(r"iids\[\]=(\d+)", m.group(1))
        out = [full(i, state["mrs"][i]) for i in wanted if i in state["mrs"]]
        print(json.dumps(out)); sys.exit(0)
    m = re.match(r"projects/:id/merge_requests\?source_branch=([^&]+)&state=(opened|all)", path)
    if m:
        from urllib.parse import unquote
        out = [full(i, mr) for i, mr in sorted(state["mrs"].items(), key=lambda kv: -int(kv[0]))
               if mr.get("source_branch") == unquote(m.group(1)) and m.group(2) in ("all", mr.get("state"))]
        print(json.dumps(out)); sys.exit(0)
    sys.stderr.write("unknown api path " + path + "\n"); sys.exit(1)
if args[0] == "mr" and args[1] == "create":
    branch = args[args.index("--source-branch") + 1]
    iid = str(max([int(i) for i in state["mrs"]] + [0]) + 1)
    state["mrs"][iid] = {"iid": int(iid), "state": "opened", "draft": True, "source_branch": branch,
                         "detailed_merge_status": "mergeable", "approvals": 0}
    json.dump(state, open(state_path, "w"))
    print("!" + iid); sys.exit(0)
sys.stderr.write("unsupported: " + " ".join(args) + "\n"); sys.exit(1)
'''


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    primary = tmp_path / "proj-framework"
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True)
    g = lambda *a: subprocess.run(["git", "-C", str(primary), *a], check=True, capture_output=True, text=True)
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (primary / "README.md").write_text("x\n")
    (primary / ".gitignore").write_text("local-docs/\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    g("push", "-q", "-u", "origin", "main")
    g("remote", "set-head", "origin", "main")
    (primary / "local-docs").mkdir()
    (primary / "local-docs" / "flow.local.yml").write_text('gate: "true"\nforge: gitlab\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    glab = bin_dir / "glab"
    glab.write_text(FAKE_GLAB)
    glab.chmod(0o755)
    state = tmp_path / "glab.json"
    state.write_text(json.dumps({"mrs": {}}))
    environ = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "FLOW_HUMAN")}
    environ.update({"PATH": f"{bin_dir}:{environ['PATH']}", "FAKE_GLAB_STATE": str(state),
                    "FLOW_BACKGROUND_SYNC": "0"})
    return {"primary": primary, "origin": origin, "env": environ, "state": state, "tmp": tmp_path}


def flow(env: dict, *args: str, cwd: Path | None = None, human: bool = False, claude: bool = False,
         check: bool = False, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(env["env"], **(extra_env or {}))
    if human:
        e["FLOW_HUMAN"] = "1"
    if claude:
        e["CLAUDECODE"] = "1"
    result = subprocess.run([sys.executable, "-m", "flow_worktrees", *args], cwd=cwd or env["primary"],
                            capture_output=True, text=True, env=e)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def set_state(env: dict, **mrs) -> None:
    data = json.loads(env["state"].read_text())
    for iid, mr in mrs.items():
        data["mrs"].setdefault(iid, {}).update(mr)
    env["state"].write_text(json.dumps(data))


def git_in(where: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(where), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                          check=check, capture_output=True, text=True)


def commit(where: Path, name: str, text: str = "x\n") -> None:
    (where / name).parent.mkdir(parents=True, exist_ok=True)
    (where / name).write_text(text)
    git_in(where, "add", name)
    git_in(where, "commit", "-q", "-m", f"touch {name}")


def push(where: Path, branch: str = "PL-7-smoke") -> None:
    git_in(where, "push", "-q", "-u", "origin", branch)


def to_test_stage(env: dict, wt: Path) -> None:
    for _ in range(3):
        flow(env, "next", cwd=wt, check=True)  # plan -> refine -> implement -> test


def read_meta(env: dict, key: str, done: bool = False) -> dict:
    path = env["primary"] / "local-docs" / "tasks" / ("done/" if done else "") / f"{key}.md"
    text = path.read_text()
    return yaml.safe_load(text.split("---\n")[1])


def age_task(env: dict, key: str, date: str = "2020-01-01") -> None:
    """Backdate `updated` on disk; `flow set` cannot, because every save re-stamps it with today."""
    path = env["primary"] / "local-docs" / "tasks" / f"{key}.md"
    path.write_text(re.sub(r"^updated: .*$", f"updated: '{date}'", path.read_text(), count=1, flags=re.M))


def calls_made(env: dict) -> list[str]:
    log = Path(str(env["state"]) + ".calls")
    return log.read_text().splitlines() if log.exists() else []


def row(env: dict, key: str = "PL-7", offline: bool = False) -> dict:
    args = ["status", "--json"] + (["--offline"] if offline else [])
    return next(r for r in json.loads(flow(env, *args, check=True).stdout) if r["key"] == key)


def stage_ball(env: dict, key: str = "PL-7") -> tuple[str, str]:
    r = row(env, key)
    return r["stage"], r["ball"]


def thread(*authors: str, resolved: bool = False, first_id: int = 1) -> dict:
    return {"notes": [{"id": first_id + i, "author": {"username": a}, "resolvable": True, "resolved": resolved,
                       "system": False} for i, a in enumerate(authors)]}


def mr(env: dict, iid: str = "5", branch: str = "PL-7-smoke", **fields) -> None:
    base = {"state": "opened", "draft": False, "detailed_merge_status": "mergeable", "approvals": 0,
            "source_branch": branch, "discussions": []}
    data = json.loads(env["state"].read_text())
    data["mrs"][iid] = {**base, **fields}
    env["state"].write_text(json.dumps(data))


def hook_context(env: dict, cwd: Path | None = None, **extra_env: str) -> str:
    result = flow(env, "status", "--hook", cwd=cwd, check=True, extra_env=extra_env)
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] if result.stdout else ""


def write_task(env: dict, key: str, **fields) -> None:
    meta = {"key": key, "title": "", "stage": "plan", "blocked_on": [], "branch": "", "worktree": "", "mr": None,
            "next": "", "updated": "2026-09-01", "docs": [], **fields}
    path = env["primary"] / "local-docs" / "tasks" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n")


def start(env: dict, key: str = "PL-7", slug: str = "smoke") -> Path:
    flow(env, "start", key, slug, "--title", "Smoke test", human=True, check=True)
    return env["tmp"] / f"proj-{key.split('-')[1]}"


# --------------------------------------------------------------------------- status / hook


def test_status_without_tasks(env: dict) -> None:
    result = flow(env, "status", "--offline", check=True)
    assert result.stdout.strip() == "no tasks"


def test_hook_is_silent_without_tasks_and_outside_repo(env: dict) -> None:
    assert flow(env, "status", "--hook", check=True).stdout == ""
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    result = flow(env, "status", "--hook", cwd=outside)
    assert result.returncode == 0 and result.stdout == ""


def test_hook_emits_session_start_context(env: dict) -> None:
    start(env)
    payload = json.loads(flow(env, "status", "--hook", check=True).stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "PL-7" in context and "plan" in context


def test_md_and_tsv_render(env: dict) -> None:
    start(env)
    assert flow(env, "status", "--md", "--offline", check=True).stdout.startswith("| Key |")
    assert flow(env, "status", "--tsv", "--offline", check=True).stdout.split("\t")[0] == "PL-7"


def test_json_render_carries_the_full_row_model(env: dict) -> None:
    start(env)
    mr(env, draft=True, web_url="https://gl/mr/5")
    assert row(env, offline=True)["mr"] == "-"  # never asked: no MR is known for the branch yet
    r = row(env)
    assert r["key"] == "PL-7" and r["age"] == 0 and r["blocked_list"] == []
    assert (r["mr_iid"], r["mr_state"], r["mr_draft"], r["mr_url"]) == (5, "opened", True, "https://gl/mr/5")
    assert (r["stage"], r["ball"], r["stage_source"]) == ("review-wait", "me", "forge")  # my draft: un-drafting is mine
    offline = row(env, offline=True)
    assert (offline["mr_state"], offline["stage"]) == ("opened", "review-wait")  # served from the cache


# The `--json` row is the contract external tools read by field name. Renaming or dropping a key breaks them
# silently, so the key set and value types are frozen here: change this table only on purpose.
JSON_ROW_TYPES: dict[str, tuple[type, ...]] = {
    "current": (bool,), "key": (str,), "title": (str,),
    "stage": (str,), "stage_source": (str,), "ball": (str,), "ball_source": (str,),
    "branch": (str,), "branch_name": (str,), "branch_ab": (str,), "wt": (str,),
    "mr": (str,), "mr_iid": (int, type(None)), "mr_state": (str, type(None)), "mr_draft": (bool, type(None)),
    "mr_url": (str, type(None)), "mr_mine": (bool,),
    "next": (str,), "updated": (str,), "updated_on": (str,), "age": (int, type(None)),
    "blocked": (str,), "blocked_list": (list,), "last_event": (dict, type(None)),
}


def test_json_row_keys_and_types_are_a_frozen_contract(env: dict) -> None:
    start(env)
    start(env, "PL-8", "second")
    mr(env, web_url="https://gl/mr/5")  # PL-7 has an MR, PL-8 has none: both shapes of every nullable field
    rows = json.loads(flow(env, "status", "--json", check=True).stdout)
    assert {r["key"] for r in rows} == {"PL-7", "PL-8"}
    for r in rows:
        assert set(r) == set(JSON_ROW_TYPES), r["key"]
        wrong = {k: type(v).__name__ for k, v in r.items() if not isinstance(v, JSON_ROW_TYPES[k])}
        assert not wrong, (r["key"], wrong)
    assert set(_flow_module().TSV_COLUMNS) <= set(JSON_ROW_TYPES)  # --tsv is a positional view of the same row


def test_html_board_writes_file_with_lanes_badges_and_blocked_links(env: dict) -> None:
    start(env)
    start(env, "PL-8", "second")
    mr(env, state="merged", web_url="https://gl/mr/5")
    flow(env, "set", "stage", "parked", "PL-8", "--blocked-on", "PL-7 merge, <vpn>", human=True, check=True)
    flow(env, "set", "ball", "me", "PL-8", "--why", "I chase the VPN ticket", check=True)
    age_task(env, "PL-8")
    out = env["tmp"] / "board.html"
    result = flow(env, "status", "--html", "--out", str(out), check=True)
    assert result.stdout.strip() == str(out)
    page = out.read_text()
    assert page.startswith("<!doctype html>") and 'id="lane-parked"' in page
    assert all(f'data-f="{f}"' in page for f in ("me", "them", "hot", "active"))
    assert '<a href="https://gl/mr/5">!5:merged</a>' in page and "mr-merged" in page
    assert '<a href="#PL-7">PL-7 merge</a>' in page and "&lt;vpn&gt;" in page  # blocked chip links a known key; escapes
    assert 'class="badge age-hot"' not in page  # parked tasks are not heat-mapped even when stale and mine
    flow(env, "set", "stage", "plan", "PL-8", check=True)
    age_task(env, "PL-8")  # every save re-stamps `updated`
    flow(env, "status", "--html", "--out", str(out), check=True)
    assert 'class="badge age-hot"' in out.read_text()
    default = flow(env, "status", "--html", "--offline", check=True).stdout.strip()
    assert default == str(env["primary"] / "local-docs" / "flow-status.html") and Path(default).exists()
    Path(default).unlink()
    result = flow(env, "board", "--offline", extra_env={"BROWSER": "true"})  # `true`: a no-op opener
    assert result.returncode == 0 and result.stdout.strip() == default and Path(default).exists()


# --------------------------------------------------------------------------- start / human guard


def test_start_is_human_only(env: dict) -> None:
    result = flow(env, "start", "PL-7", "smoke", claude=True)
    assert result.returncode == 3 and "human-only" in result.stderr
    assert flow(env, "start", "PL-7", "smoke", claude=True, human=True).returncode == 0


def test_start_creates_worktree_branch_symlink_task(env: dict) -> None:
    wt = start(env)
    assert wt.is_dir()
    assert (wt / "local-docs").is_symlink()
    assert (wt / "local-docs" / "flow.local.yml").exists()
    branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    assert branch == "PL-7-smoke"
    meta = read_meta(env, "PL-7")
    assert meta["stage"] == "plan" and meta["branch"] == "PL-7-smoke" and meta["worktree"] == "../proj-7"
    assert meta["title"] == "Smoke test"


def test_start_rejects_bad_key_and_slug(env: dict) -> None:
    assert flow(env, "start", "pl-7", "smoke", human=True).returncode == 1
    assert flow(env, "start", "PL-7", "Bad_Slug", human=True).returncode == 1


# --------------------------------------------------------------------------- set / note / next


def test_set_and_note_update_fields_and_date(env: dict) -> None:
    wt = start(env)
    flow(env, "set", "ball", "them", "--why", "waiting on Slack", cwd=wt, check=True)
    flow(env, "note", "looked at foo", "--next", "do bar", cwd=wt, check=True)
    meta = read_meta(env, "PL-7")
    assert meta["ball_pin"]["value"] == "them" and "ball" not in meta and meta["next"] == "do bar"
    body = (env["primary"] / "local-docs" / "tasks" / "PL-7.md").read_text().split("---\n", 2)[2]
    assert ": looked at foo" in body


def test_set_rejects_unknown_field_and_stage(env: dict) -> None:
    start(env)
    assert flow(env, "set", "colour", "red", "PL-7").returncode == 1
    assert flow(env, "set", "stage", "flying", "PL-7").returncode == 1
    assert flow(env, "set", "stage", "cleaned", "PL-7").returncode == 2


def test_parking_is_human_only_and_needs_reason(env: dict) -> None:
    start(env)
    assert flow(env, "set", "stage", "parked", "PL-7", claude=True).returncode == 3
    assert flow(env, "set", "stage", "parked", "PL-7", human=True).returncode == 2
    flow(env, "set", "stage", "parked", "PL-7", "--blocked-on", "PL-1", human=True, check=True)
    assert read_meta(env, "PL-7")["blocked_on"] == ["PL-1"]


def test_next_walks_work_stages_from_branch(env: dict) -> None:
    wt = start(env)
    for expected in ("refine", "implement", "test"):
        flow(env, "next", cwd=wt, check=True)
        assert read_meta(env, "PL-7")["stage"] == expected


def test_next_from_test_requires_push(env: dict) -> None:
    wt = start(env)
    to_test_stage(env, wt)
    result = flow(env, "next", cwd=wt)
    assert result.returncode == 2 and "not on origin" in result.stderr
    assert read_meta(env, "PL-7")["stage"] == "test"


def test_next_from_test_creates_draft_mr(env: dict) -> None:
    wt = start(env)
    to_test_stage(env, wt)
    push(wt)
    result = flow(env, "next", cwd=wt, check=True)
    assert "created Draft MR !1" in result.stdout and "test → review-wait" in result.stdout
    meta = read_meta(env, "PL-7")
    assert meta["stage"] == "test" and meta["mr"] is None and "ball" not in meta
    assert stage_ball(env) == ("review-wait", "me")  # a draft waits on me to un-draft it
    assert json.loads(env["state"].read_text())["mrs"]["1"]["source_branch"] == "PL-7-smoke"
    assert "un-draft !1 (PL-7)" in hook_context(env)


def test_failing_gate_blocks_review(env: dict) -> None:
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\ngate: "false"\n')
    wt = start(env)
    to_test_stage(env, wt)
    push(wt)
    result = flow(env, "next", cwd=wt)
    assert result.returncode == 2 and "gate failed" in result.stderr
    assert read_meta(env, "PL-7")["stage"] == "test"
    silent = flow(env, "next", "--no-gate", cwd=wt)
    assert silent.returncode == 2 and "--no-gate needs --why" in silent.stderr
    assert flow(env, "next", "--why", "orphan reason", cwd=wt).returncode == 2
    flow(env, "next", "--no-gate", "--why", "toolchain older than the project needs", cwd=wt, check=True)
    notes = (env["primary"] / "local-docs" / "tasks" / "PL-7.md").read_text()
    assert notes.count("gate skipped (--no-gate): toolchain older than the project needs") == 1


def test_skip_reason_is_not_noted_when_review_stops_short(env: dict) -> None:
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\ngate: "false"\n')
    wt = start(env)
    to_test_stage(env, wt)  # not pushed: to_review refuses after the skipped gate
    result = flow(env, "next", "--no-gate", "--why", "never reached review", cwd=wt)
    assert result.returncode == 2 and "not on origin" in result.stderr
    assert "gate skipped" not in (env["primary"] / "local-docs" / "tasks" / "PL-7.md").read_text()


def test_stage_and_ball_follow_the_mr_when_i_am_the_author(env: dict) -> None:
    start(env)
    mr(env)
    assert stage_ball(env) == ("review-wait", "them")
    mr(env, discussions=[thread("rev")])
    assert stage_ball(env) == ("changes", "me")
    mr(env, discussions=[thread("rev", "me")])
    assert stage_ball(env) == ("review-wait", "them")  # I answered last in every open thread
    mr(env, approvals=1)
    assert stage_ball(env) == ("merge-wait", "them")
    mr(env, approvals=1, detailed_merge_status="conflict")
    assert stage_ball(env) == ("changes", "me")
    mr(env, state="merged")
    assert stage_ball(env) == ("merged", "me")
    assert read_meta(env, "PL-7")["stage"] == "plan"  # none of it was written to the task file
    assert flow(env, "next", "PL-7").returncode == 2  # merged -> cleaned only via clean


def test_stage_and_ball_follow_the_mr_when_i_am_the_reviewer(env: dict) -> None:
    start(env)
    author = {"author": {"username": "someone"}}
    mr(env, **author)
    assert stage_ball(env) == ("review-wait", "me")  # nobody has spoken: my first pass is owed
    mr(env, discussions=[thread("me")], **author)
    assert stage_ball(env) == ("review-wait", "them")
    mr(env, discussions=[thread("me", "someone")], **author)
    assert stage_ball(env) == ("changes", "me")
    mr(env, draft=True, **author)
    assert stage_ball(env) == ("review-wait", "them")  # their draft: they un-draft


def test_offline_glab_is_a_precondition_failure_not_a_crash(env: dict) -> None:
    start(env)
    mr(env, approvals=1)
    flow(env, "sync", check=True)
    data = json.loads(env["state"].read_text())
    env["state"].write_text(json.dumps({**data, "offline": True}))
    result = flow(env, "next", "PL-7")
    assert result.returncode == 2 and "offline" in result.stderr
    status = flow(env, "status", "--json", check=True)
    assert "from cache" in status.stderr and json.loads(status.stdout)[0]["stage"] == "merge-wait"


# --------------------------------------------------------------------------- clean


def test_clean_refuses_unmerged_and_dirty(env: dict) -> None:
    wt = start(env)
    assert flow(env, "clean", "PL-7", claude=True).returncode == 3
    result = flow(env, "clean", "PL-7", human=True)
    assert result.returncode == 2 and "cannot prove it merged" in result.stderr
    (wt / "junk.txt").write_text("x")
    result = flow(env, "clean", "PL-7", "--force-unmerged", human=True)
    assert result.returncode == 2 and "uncommitted" in result.stderr
    assert wt.exists()


def test_clean_removes_worktree_branch_and_archives(env: dict) -> None:
    wt = start(env)
    mr(env, "9", state="merged")
    result = flow(env, "clean", "PL-7", human=True, check=True)
    assert not wt.exists()
    branches = subprocess.run(["git", "-C", str(env["primary"]), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    assert "PL-7-smoke" not in branches
    assert not (env["primary"] / "local-docs" / "tasks" / "PL-7.md").exists()
    meta = read_meta(env, "PL-7", done=True)
    assert meta["stage"] == "cleaned" and meta["worktree"] == ""
    assert "pulled main" in result.stdout


def test_clean_refuses_from_inside_the_worktree(env: dict) -> None:
    wt = start(env)
    result = flow(env, "clean", "PL-7", "--force-unmerged", human=True, cwd=wt)
    assert result.returncode == 1 and "primary checkout" in result.stderr


# --------------------------------------------------------------------------- doctor


def test_doctor_finds_and_fixes_missing_symlink(env: dict) -> None:
    wt = start(env)
    (wt / "local-docs").unlink()
    result = flow(env, "doctor", "--offline")
    assert result.returncode == 1 and "no local-docs symlink" in result.stdout
    assert flow(env, "doctor", "--offline", "--fix", check=False).returncode == 1
    assert (wt / "local-docs").is_symlink()
    assert flow(env, "doctor", "--offline", check=True).stdout.strip().endswith("ok")


def test_doctor_reports_branch_without_task(env: dict) -> None:
    subprocess.run(["git", "-C", str(env["primary"]), "branch", "PL-99-orphan"], check=True)
    result = flow(env, "doctor", "--offline")
    assert result.returncode == 1 and "PL-99-orphan: no task file" in result.stdout


def test_md_write_replaces_snapshot_block(env: dict) -> None:
    start(env)
    plan = env["primary"] / "local-docs" / "PLAN.local.md"
    plan.write_text("# plan\n\n## 1. Snapshot\n\n<!-- plan:snapshot:begin -->\nold\n<!-- plan:snapshot:end -->\n\n## 2. Rest\n")
    flow(env, "status", "--md", "--write", "--offline", check=True)
    text = plan.read_text()
    assert "old" not in text and "| PL-7 |" in text and text.endswith("## 2. Rest\n")
    plan.write_text("# plan without markers\n")
    assert flow(env, "status", "--md", "--write", "--offline").returncode == 1


# --------------------------------------------------------------------------- review fixes (2026-09-09)


def _flow_module():
    return importlib.import_module("flow_worktrees.cli")


def test_glab_fails_fast_after_a_connectivity_failure_but_not_after_404(monkeypatch, tmp_path: Path) -> None:
    mod = _flow_module()
    calls: list[list[str]] = []

    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(mod.subprocess, "run", timeout)
    glab = mod.Glab(tmp_path)
    for _ in range(3):
        with pytest.raises(mod.FlowError) as error:
            glab.api("projects/:id")
        assert error.value.code == 2 and "offline" in str(error.value)
    assert len(calls) == 1  # the second and third call never reached subprocess

    class NotFound:
        returncode, stdout, stderr = 1, "", "GET .../merge_requests/4012: 404 Not Found\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: calls.append(args) or NotFound())
    glab = mod.Glab(tmp_path)
    for _ in range(2):
        with pytest.raises(mod.FlowError) as error:
            glab.api("projects/:id/merge_requests/4012")
        assert "auth" not in str(error.value)  # `4012` must not read as an auth failure
    assert len(calls) == 3 and glab.dead is None


def test_status_offline_glab_warns_once_and_falls_back_to_the_cache(env: dict) -> None:
    start(env)
    start(env, "PL-8", "second")
    env["state"].write_text(json.dumps({"mrs": {}, "offline": True}))
    result = flow(env, "status", check=True)
    assert result.stderr.count("glab offline") == 1 and "no cache yet" in result.stderr
    env["state"].write_text(json.dumps({"mrs": {}}))
    mr(env, "5")
    mr(env, "6", branch="PL-8-second")
    flow(env, "sync", check=True)
    env["state"].write_text(json.dumps({**json.loads(env["state"].read_text()), "offline": True}))
    result = flow(env, "status", check=True)
    assert "!5:opened" in result.stdout and "!6:opened" in result.stdout
    assert result.stderr.count("glab offline") == 1 and "from cache" in result.stderr


def test_stored_mrs_of_branchless_tasks_are_batched_and_unknown_iids_stay_questions(env: dict) -> None:
    write_task(env, "PL-20", mr=5)
    write_task(env, "PL-21", mr=6)  # never created on the forge
    mr(env, "5", branch="elsewhere")
    result = flow(env, "status", check=True)
    assert "!5:opened" in result.stdout and "!6?" in result.stdout and result.stderr == ""
    calls = calls_made(env)
    assert sum("iids[]" in c for c in calls) == 1


def test_broken_task_file_is_skipped_with_a_warning_and_hook_stays_valid_json(env: dict) -> None:
    start(env)
    tasks = env["primary"] / "local-docs" / "tasks"
    (tasks / "PL-9.md").write_text("no frontmatter here\n")
    (tasks / "PL-10.md").write_text("---\nkey: PL-11\nstage: plan\n---\n")
    (tasks / "PL-12.md").write_text("---\nkey: [unclosed\n---\n")
    result = flow(env, "status", "--hook", check=True)
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "PL-7" in context and "PL-9" not in context and "PL-10" not in context and "PL-11" not in context
    assert "skipping" in result.stderr and "missing YAML frontmatter" in result.stderr
    assert "does not match the filename" in result.stderr and "invalid frontmatter YAML" in result.stderr
    assert "Traceback" not in result.stderr
    doctor = flow(env, "doctor", "--offline")
    assert "Traceback" not in doctor.stderr and "skipping" in doctor.stderr
    direct = flow(env, "set", "title", "x", "PL-10")
    assert direct.returncode == 1 and "does not match the filename" in direct.stderr


def test_hook_reports_its_reason_on_stderr_while_exiting_zero(env: dict) -> None:
    start(env)
    (env["primary"] / "local-docs" / "flow.local.yml").write_text("gate: [unclosed\n")
    result = flow(env, "status", "--hook")
    assert result.returncode == 0 and result.stdout == ""
    assert "invalid YAML" in result.stderr and "Traceback" not in result.stderr


def test_bad_user_input_is_a_flow_error_not_a_traceback(env: dict) -> None:
    start(env)
    for value in ("abc", "!x", "0", "-3"):
        result = flow(env, "set", "mr", value, "PL-7")
        assert result.returncode == 1 and "Traceback" not in result.stderr, value
        assert "mr must be" in result.stderr or "must be positive" in result.stderr
    assert read_meta(env, "PL-7")["mr"] is None
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\ngate: "echo {nope}"\n')
    for _ in range(3):
        flow(env, "next", "PL-7", check=True)  # plan -> refine -> implement -> test
    result = flow(env, "next", "PL-7")
    assert result.returncode == 1 and "bad placeholder" in result.stderr and "{base}" in result.stderr
    assert "Traceback" not in result.stderr
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\nworktree_dir: "../wt-{0}"\n')
    result = flow(env, "start", "PL-8", "bad-template", human=True)
    assert result.returncode == 1 and "worktree_dir" in result.stderr and "Traceback" not in result.stderr
    result = flow(env, "status", "--md", "--json", "--offline")
    assert result.returncode == 2 and "not allowed with" in result.stderr


def test_clean_archives_the_task_even_when_the_pull_fails(env: dict) -> None:
    wt = start(env)
    mr(env, "9", state="merged")
    commit(wt, "remote.txt")
    git_in(wt, "push", "-q", "origin", "HEAD:main")
    commit(env["primary"], "local.txt")  # main has diverged: --ff-only must fail
    result = flow(env, "clean", "PL-7", human=True)
    assert result.returncode == 0, result.stderr
    assert not wt.exists() and not (env["primary"] / "local-docs" / "tasks" / "PL-7.md").exists()
    assert read_meta(env, "PL-7", done=True)["stage"] == "cleaned"
    assert "pull did not happen" in result.stderr and "pulled main" not in result.stdout


def test_flow_today_overrides_the_clock(env: dict) -> None:
    start(env)
    result = flow(env, "set", "next", "x", "PL-7", extra_env={"FLOW_TODAY": "2020-01-01"})
    assert result.returncode == 0, result.stderr
    assert str(read_meta(env, "PL-7")["updated"]) == "2020-01-01"
    assert "2020" not in flow(env, "status", "--offline", check=True).stdout  # real clock: shown as an age
    result = flow(env, "status", "--offline", extra_env={"FLOW_TODAY": "yesterday"})
    assert result.returncode == 1 and "FLOW_TODAY" in result.stderr and "Traceback" not in result.stderr


def test_approvals_required_is_configurable(env: dict) -> None:
    start(env)
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\ngate: "true"\napprovals_required: 2\n')
    mr(env, approvals=1)
    assert stage_ball(env) == ("review-wait", "them")  # one of two: the second reviewer owes an approval
    mr(env, approvals=2)
    assert stage_ball(env) == ("merge-wait", "them")


# --------------------------------------------------------------------------- 2026-09-23 improvements


def test_note_does_not_stamp_the_date_twice(env: dict) -> None:
    wt = start(env)
    flow(env, "note", "2026-09-23: rebased onto main", cwd=wt, check=True)
    flow(env, "note", "2026-09-23 plain date prefix", cwd=wt, check=True)
    flow(env, "note", "keeps 2026-09-23 inside the text", cwd=wt, check=True)
    body = (env["primary"] / "local-docs" / "tasks" / "PL-7.md").read_text().split("---\n", 2)[2]
    lines = [line for line in body.splitlines() if line.startswith("- ")]
    assert [line.split(": ", 1)[1] for line in lines] == [
        "rebased onto main", "plain date prefix", "keeps 2026-09-23 inside the text",
    ]


def test_branch_column_says_whether_the_branch_is_pushed(env: dict) -> None:
    wt = start(env)
    column = lambda: flow(env, "status", "--tsv", "--offline", check=True).stdout.split("\t")[4]
    assert column().endswith("+0/-0 local")
    push(wt)
    assert column().endswith("+0/-0")
    (wt / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(wt), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "f"],
                   check=True)
    assert column().endswith("+1/-0 ⇡1")


def test_hook_prints_the_full_next_of_the_current_task_only(env: dict) -> None:
    wt = start(env)
    start(env, "PL-8", "other")
    long_next = "step " * 20 + "END"
    flow(env, "set", "next", long_next, "PL-7", check=True)
    flow(env, "set", "next", "other " * 20 + "TAIL", "PL-8", check=True)
    context = json.loads(flow(env, "status", "--hook", cwd=wt, check=True).stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert f"* PL-7 next (full): {long_next}" in context
    assert "TAIL" not in context


def test_hook_lists_the_human_commands_from_the_cache(env: dict) -> None:
    start(env)
    start(env, "PL-8", "second")
    assert "for the human" not in hook_context(env)
    mr(env, "5", state="merged")
    mr(env, "6", branch="PL-8-second", draft=True)
    flow(env, "sync", check=True)
    context = hook_context(env)
    assert "for the human (own terminal): flow clean PL-7; un-draft !6 (PL-8) in GitLab" in context


def test_a_ball_pin_holds_until_the_mr_moves_and_doctor_names_it_then(env: dict) -> None:
    start(env)
    mr(env)
    flow(env, "sync", check=True)
    assert flow(env, "set", "ball", "me", "PL-7").returncode == 1  # a pin needs a reason
    flow(env, "set", "ball", "me", "PL-7", "--why", "reviewer asked on Slack", check=True)
    r = row(env)
    assert (r["ball"], r["ball_source"]) == ("me", "pin")
    assert flow(env, "doctor", "--offline", check=True).stdout.strip().endswith("ok")
    mr(env, discussions=[thread("rev", "me")])
    r = row(env)
    assert (r["ball"], r["ball_source"]) == ("them", "derived")
    result = flow(env, "doctor", "--offline")
    assert result.returncode == 1 and "the MR moved since" in result.stdout
    flow(env, "set", "ball", "auto", "PL-7", check=True)
    assert "ball_pin" not in read_meta(env, "PL-7")


def test_doctor_asks_for_clean_only_once_merged_is_stale(env: dict) -> None:
    start(env)
    mr(env, state="merged", merged_at=datetime.date.today().isoformat() + "T10:00:00Z")
    assert "not cleaned" not in flow(env, "doctor").stdout
    mr(env, state="merged", merged_at="2020-01-01T10:00:00Z")
    result = flow(env, "doctor")
    assert result.returncode == 1 and "flow clean PL-7" in result.stdout


# --------------------------------------------------------------------------- overlaps


def advance_main(env: dict, name: str) -> None:
    commit(env["primary"], name, "main\n")
    git_in(env["primary"], "push", "-q", "origin", "main")


def overlaps(env: dict) -> list[str]:
    out = flow(env, "status", "--offline", check=True).stdout
    return [line.strip() for line in out.split("overlaps:", 1)[1].splitlines() if line.strip()] if "overlaps:" in out \
        else []


def test_main_moving_under_a_branch_names_the_commit_and_the_shared_files(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    advance_main(env, "b.txt")
    assert overlaps(env) == []
    advance_main(env, "a.txt")
    [line] = overlaps(env)
    assert line.startswith("PL-7 <- main moved under it:") and "touch a.txt" in line and line.endswith("a.txt")


def test_two_branches_changing_one_file_are_reported_and_disjoint_ones_are_not(env: dict) -> None:
    first, second = start(env), start(env, "PL-8", "other")
    commit(first, "a.txt")
    commit(second, "b.txt")
    assert overlaps(env) == []
    commit(second, "a.txt", "y\n")
    assert overlaps(env) == ["PL-7 <-> PL-8: both change a.txt"]


def test_a_branch_built_on_another_is_stacked_not_overlapping(env: dict) -> None:
    lower, upper = start(env), start(env, "PL-8", "upper")
    commit(lower, "a.txt")
    subprocess.run(["git", "-C", str(upper), "reset", "-q", "--hard", "PL-7-smoke"], check=True)
    commit(upper, "b.txt")
    assert overlaps(env) == ["PL-8 is stacked on PL-7"]
    commit(lower, "c.txt")
    assert overlaps(env) == ["PL-7 and PL-8 share 1 unmerged commit(s): one is built on the other, "
                             "and the lower one has moved on since"]


def test_a_branch_without_commits_is_not_mistaken_for_a_stack_base(env: dict) -> None:
    start(env)  # PL-7 stays at the old main commit, an ancestor of everything created later
    advance_main(env, "b.txt")
    later = start(env, "PL-8", "later")
    commit(later, "a.txt")
    assert overlaps(env) == []


def test_overlap_ignore_drops_files_whose_sharing_is_not_a_conflict(env: dict) -> None:
    first, second = start(env), start(env, "PL-8", "other")
    commit(first, ".metrics/ci.jsonl")
    commit(second, ".metrics/ci.jsonl", "y\n")
    assert overlaps(env) == ["PL-7 <-> PL-8: both change .metrics/ci.jsonl"]
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: gitlab\ngate: "true"\noverlap_ignore: [".metrics/*"]\n')
    assert overlaps(env) == []


def test_hook_carries_the_overlaps(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    advance_main(env, "a.txt")
    context = json.loads(flow(env, "status", "--hook", check=True).stdout)["hookSpecificOutput"]["additionalContext"]
    assert "overlaps:\n  PL-7 <- main moved under it:" in context


# --------------------------------------------------------------------------- edge-case hardening (2026-09-23)


def test_clean_refuses_to_delete_commits_that_exist_nowhere_else(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    push(wt)
    tip = git_in(wt, "rev-parse", "HEAD").stdout.strip()
    mr(env, "9", state="merged", sha=tip)
    commit(wt, "late-fix.txt")  # after the merge, never pushed
    result = flow(env, "clean", "PL-7", human=True)
    assert result.returncode == 2 and "touch late-fix.txt" in result.stderr and wt.exists()
    result = flow(env, "clean", "PL-7", "--force-unmerged", human=True)
    assert result.returncode == 2 and wt.exists()  # --force-unmerged does not license losing commits
    flow(env, "clean", "PL-7", "--discard-commits", human=True, check=True)
    assert not wt.exists()


def test_clean_accepts_a_branch_whose_remote_copy_was_deleted_after_merge(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    push(wt)
    tip = git_in(wt, "rev-parse", "HEAD").stdout.strip()
    git_in(env["origin"], "branch", "-D", "PL-7-smoke")  # GitLab removed the source branch on merge
    git_in(env["primary"], "fetch", "-q", "--prune", "origin")
    mr(env, "9", state="merged", sha=tip)
    flow(env, "clean", "PL-7", human=True, check=True)
    assert not wt.exists()


def test_doctor_finds_and_fixes_a_dangling_symlink(env: dict) -> None:
    wt = start(env)
    (wt / "local-docs").unlink()
    (wt / "local-docs").symlink_to(env["tmp"] / "moved-away", target_is_directory=True)
    result = flow(env, "doctor", "--offline")
    assert result.returncode == 1 and "local-docs" in result.stdout and "dangling" in result.stdout
    flow(env, "doctor", "--offline", "--fix")
    assert (wt / "local-docs").resolve() == (env["primary"] / "local-docs").resolve()
    assert flow(env, "doctor", "--offline", check=True).stdout.strip().endswith("ok")


def test_next_refuses_when_the_branch_is_gone_from_origin_despite_a_stale_tracking_ref(env: dict) -> None:
    wt = start(env)
    to_test_stage(env, wt)
    push(wt)
    git_in(env["origin"], "branch", "-D", "PL-7-smoke")  # deleted server-side; local origin/PL-7-smoke is stale
    result = flow(env, "next", cwd=wt)
    assert result.returncode == 2 and "not on origin" in result.stderr
    assert read_meta(env, "PL-7")["stage"] == "test"


def test_next_runs_the_gate_where_the_branch_is_checked_out_not_where_the_task_says(env: dict) -> None:
    (env["primary"] / "local-docs" / "flow.local.yml").write_text(
        'forge: gitlab\ngate: test "$(git branch --show-current)" = PL-7-smoke\n')
    wt = start(env)
    to_test_stage(env, wt)
    push(wt)
    flow(env, "set", "worktree", "", "PL-7", check=True)  # stale field: says primary, git says the worktree
    doctor = flow(env, "doctor", "--offline")
    assert doctor.returncode == 1 and "checked out in" in doctor.stdout
    result = flow(env, "next", "PL-7")
    assert result.returncode == 0, result.stderr
    assert stage_ball(env)[0] == "review-wait"


def test_next_refuses_a_branch_that_is_not_checked_out_anywhere(env: dict) -> None:
    wt = start(env)
    to_test_stage(env, wt)
    push(wt)
    git_in(wt, "checkout", "-q", "--detach")
    result = flow(env, "next", "PL-7")
    assert result.returncode == 2 and "not checked out" in result.stderr


def test_next_refuses_mid_merge_and_warns_on_conflicts_with_main(env: dict) -> None:
    wt = start(env)
    to_test_stage(env, wt)
    commit(wt, "a.txt", "branch\n")
    push(wt)
    advance_main(env, "a.txt")
    git_in(wt, "fetch", "-q", "origin")
    result = flow(env, "next", cwd=wt)
    assert result.returncode == 0, result.stderr
    assert "conflicts with origin/main" in result.stderr and "a.txt" in result.stderr
    set_state(env, **{"1": {"discussions": [thread("rev")]}})  # review came back: stage is changes
    assert git_in(wt, "merge", "origin/main", check=False).returncode != 0  # leaves MERGE_HEAD
    result = flow(env, "next", cwd=wt)
    assert result.returncode == 2 and "merge in progress" in result.stderr


def test_glab_403_is_reported_as_access_but_does_not_kill_later_calls(monkeypatch, tmp_path: Path) -> None:
    mod = _flow_module()

    class Forbidden:
        returncode, stdout, stderr = 1, "", "GET .../approvals: 403 Forbidden\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: Forbidden())
    glab = mod.Glab(tmp_path)
    with pytest.raises(mod.FlowError) as error:
        glab.api("projects/:id/merge_requests/1/approvals")
    assert "403" in str(error.value) and "access" in str(error.value) and glab.dead is None


def test_single_line_fields_reject_control_characters_and_md_escapes_pipes(env: dict) -> None:
    start(env)
    result = flow(env, "set", "title", "two\nlines", "PL-7")
    assert result.returncode == 1 and "single line" in result.stderr
    assert flow(env, "note", "x", "PL-7", "--next", "a\tb").returncode == 1
    flow(env, "set", "title", "a | b", "PL-7", check=True)
    md = flow(env, "status", "--md", "--offline", check=True).stdout
    assert "a \\| b" in md


def test_worktrees_keep_slashes_in_branch_names(env: dict) -> None:
    git_in(env["primary"], "worktree", "add", "-q", "-b", "feature/x", str(env["tmp"] / "fx"))
    mod = _flow_module()
    branches = set(mod.Repo(env["primary"]).worktrees.values())
    assert "feature/x" in branches


# --------------------------------------------------------------------------- derivation (2026-09-23, Part A)


def test_set_refuses_forge_stages_and_a_stored_mr_on_a_branch_task(env: dict) -> None:
    start(env)
    result = flow(env, "set", "stage", "review-wait", "PL-7")
    assert result.returncode == 2 and "comes from the MR" in result.stderr
    result = flow(env, "set", "mr", "5", "PL-7")
    assert result.returncode == 2 and "found by branch" in result.stderr
    write_task(env, "PL-20")
    flow(env, "set", "mr", "5", "PL-20", check=True)  # branchless: the only way to know its MR
    assert read_meta(env, "PL-20")["mr"] == 5


def test_migrate_drops_derived_fields_and_turns_a_disagreeing_ball_into_a_pin(env: dict) -> None:
    start(env)
    start(env, "PL-8", "second")
    tasks = env["primary"] / "local-docs" / "tasks"
    for key, ball in (("PL-7", "me"), ("PL-8", "them")):
        path = tasks / f"{key}.md"
        text = path.read_text().replace("stage: plan", "stage: review-wait")
        path.write_text(text.replace("blocked_on:", f"ball: {ball}\nblocked_on:"))
    path = tasks / "PL-7.md"
    path.write_text(path.read_text().replace("mr: null", "mr: 5"))
    mr(env, "5")
    mr(env, "6", branch="PL-8-second")
    assert flow(env, "migrate", "--dry-run", human=True).returncode == 2  # no cache: cannot know the MRs
    assert "legacy stored stage/ball" in flow(env, "doctor").stdout
    assert flow(env, "migrate", "--dry-run", claude=True).returncode == 3
    before = path.read_text()
    dry = flow(env, "migrate", "--dry-run", human=True, check=True).stdout
    assert "PL-7:" in dry and "stage: 'review-wait' -> 'test'" in dry and "mr: 5 -> None" in dry
    assert "ball: stored me, !5 says them -- to keep yours: flow set ball me PL-7" in dry
    assert path.read_text() == before
    write_task(env, "PL-30", stage="parked", blocked_on=["someone"], ball="me")  # no MR: nothing contradicts it
    flow(env, "migrate", "--apply", human=True, check=True)
    seven, eight, thirty = read_meta(env, "PL-7"), read_meta(env, "PL-8"), read_meta(env, "PL-30")
    assert seven["stage"] == "test" and seven["mr"] is None and "ball" not in seven
    assert "ball_pin" not in seven  # the MR had evidence against the stored ball: it drifted, not pinned
    assert "ball_pin" not in eight and "ball" not in eight  # stored `them` agreed: nothing to keep
    assert thirty["ball_pin"]["value"] == "me" and "ball" not in thirty
    assert stage_ball(env) == ("review-wait", "them")
    assert flow(env, "migrate", "--dry-run", human=True, check=True).stdout.strip() == \
        "0 file(s) would change; --apply to write"


def test_hook_reads_the_cache_without_network_and_says_how_old_it_is(env: dict) -> None:
    start(env)
    mr(env, discussions=[thread("rev")])
    flow(env, "sync", check=True)
    cache = env["primary"] / "local-docs" / ".flow-cache" / "forge.json"
    data = json.loads(cache.read_text())
    cache.write_text(json.dumps({**data, "fetched_at": data["fetched_at"] - 7200}))
    calls = len(calls_made(env))
    context = hook_context(env)
    assert "changes" in context and "forge: cache 2h old" in context
    assert len(calls_made(env)) == calls  # the hook never asked GitLab


def test_hook_starts_one_background_sync_for_a_stale_cache(env: dict) -> None:
    start(env)
    mr(env, approvals=1)
    first = hook_context(env, FLOW_BACKGROUND_SYNC="1")
    second = hook_context(env, FLOW_BACKGROUND_SYNC="1")
    assert "background sync started" in first and "background sync started" not in second  # the lock holds
    cache = env["primary"] / "local-docs" / ".flow-cache" / "forge.json"
    deadline = time.time() + 20
    while not cache.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert cache.exists(), (env["primary"] / "local-docs" / ".flow-cache" / "sync.log").read_text()
    assert "merge-wait" in hook_context(env)


def test_derivation_rules_as_a_table(tmp_path: Path) -> None:
    mod = _flow_module()

    Cache = lambda entry: mod.ForgeCache.from_data({"me": "me", "by_branch": {"b": 1},
                                                    "mrs": {"1": entry} if entry else {}})

    def entry(**kw):
        base = {"state": "opened", "draft": False, "author": "me", "merge_status": "mergeable", "approvals": 0,
                "threads": [], "merged_at": ""}
        return {**base, **kw}

    t = lambda author, resolved=False: {"resolved": resolved, "last_author": author, "last_note_id": 1}
    meta = {"stage": "test", "branch": "b", "mr": None, "ball": None, "ball_pin": None}
    cases = [
        (None, "test", "me"),
        (entry(state="merged"), "merged", "me"),
        (entry(state="closed"), "test", "me"),
        (entry(draft=True), "review-wait", "me"),
        (entry(draft=True, author="x"), "review-wait", "them"),
        (entry(draft=True, threads=[t("x")]), "changes", "me"),
        (entry(approvals=1), "merge-wait", "them"),
        (entry(approvals=1, threads=[t("x")]), "changes", "me"),
        (entry(approvals=1, merge_status="conflict"), "changes", "me"),
        (entry(threads=[t("x")]), "changes", "me"),
        (entry(threads=[t("x", resolved=True)]), "review-wait", "them"),
        (entry(threads=[t("me")]), "review-wait", "them"),
        (entry(author="x"), "review-wait", "me"),
        (entry(author="x", threads=[t("me")]), "review-wait", "them"),
    ]
    for e, stage, ball in cases:
        d = mod.derive(meta, Cache(e), 1)
        assert (d.stage, d.ball) == (stage, ball), e
    assert mod.derive({**meta, "stage": "parked"}, Cache(entry()), 1).stage == "parked"
    closed = mod.derive(meta, Cache(entry(state="closed")), 1)
    assert closed.closed and closed.stage_source == "stored"



# --------------------------------------------------------------------------- journal (2026-09-23, Part B)


def journal(env: dict, key: str = "PL-7", done: bool = False) -> list[dict]:
    path = env["primary"] / "local-docs" / "tasks" / ("done" if done else "") / f"{key}.events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_journal_records_commits_amends_pushes_and_rebases_once(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    git_in(wt, "commit", "-q", "--amend", "-m", "touch a.txt, amended")
    push(wt)
    advance_main(env, "b.txt")
    git_in(wt, "fetch", "-q", "origin")
    git_in(wt, "rebase", "-q", "origin/main")
    flow(env, "status", "--offline", check=True)
    assert journal(env) == []  # status never journals: it would cost the hook two reflog reads per task
    flow(env, "log", cwd=wt, check=True)
    first = journal(env)
    flow(env, "log", cwd=wt, check=True)
    assert journal(env) == first  # idempotent: nothing appended twice
    kinds = [e["kind"] for e in first]
    assert {"branch", "commit", "amend", "push", "rebase"} <= set(kinds)
    assert len({e["id"] for e in first}) == len(first)
    assert row(env, offline=True)["last_event"] == journal(env)[-1]  # read from the file, not from git


def test_clean_keeps_the_journal_the_branch_reflog_would_have_taken(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    push(wt)
    mr(env, "9", state="merged", sha=git_in(wt, "rev-parse", "HEAD").stdout.strip())
    flow(env, "clean", "PL-7", human=True, check=True)  # never collected before: clean must do it itself
    archived = journal(env, done=True)
    assert any(e["kind"] == "commit" and "touch a.txt" in e["text"] for e in archived)
    assert journal(env) == []


def test_log_interleaves_notes_git_and_the_gitlab_events_worth_a_line(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    note = lambda i, body, system=True, who="me": {"id": i, "system": system, "body": body,
                                                   "author": {"username": who}, "created_at": "2026-09-20T10:00:00Z"}
    mr(env, created_at="2026-09-19T09:00:00Z", discussions=[
        {"notes": [note(101, "requested review from @rev")]},
        {"notes": [note(102, "added 1 commit\n\n<ul><li>abc</li></ul>")]},
        {"notes": [note(103, "please rename **x**", system=False, who="rev")]},
    ])
    flow(env, "sync", check=True)
    flow(env, "note", "chose ledger over derivation", cwd=wt, check=True)
    out = flow(env, "log", "PL-7", check=True).stdout
    assert "mr-created" in out and "requested review from @rev" in out
    assert "!5 rev: please rename x" in out and "added 1 commit" not in out
    assert "note        chose ledger over derivation" in out and "touch a.txt" in out
    assert "requested review" not in flow(env, "log", "PL-7", "--since", "2099-01-01", check=True).stdout


def test_journal_survives_a_torn_and_a_duplicated_line(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    flow(env, "log", "PL-7", check=True)
    path = env["primary"] / "local-docs" / "tasks" / "PL-7.events.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines + [lines[-1], '{"id": "torn'] ) + "\n")  # two sessions appended at once
    out = flow(env, "log", "PL-7", check=True).stdout
    assert out.count("touch a.txt") == 1


def test_sync_journals_every_task(env: dict) -> None:
    wt = start(env)
    commit(wt, "a.txt")
    flow(env, "sync", check=True)
    assert any(e["kind"] == "commit" for e in journal(env))


def test_writing_the_snapshot_to_a_missing_file_is_an_error_not_a_traceback(env: dict) -> None:
    start(env)
    result = flow(env, "status", "--md", "--write", "--offline")
    assert result.returncode == 1 and "does not exist" in result.stderr and "Traceback" not in result.stderr
    other = env["tmp"] / "notes.md"
    other.write_text("<!-- plan:snapshot:begin -->\n<!-- plan:snapshot:end -->\n")
    flow(env, "status", "--md", "--write", str(other), "--offline", check=True)
    assert "| PL-7 |" in other.read_text()


def test_the_hook_asks_git_a_bounded_number_of_questions(env: dict) -> None:
    """Guards the review's hot-path finding: ref questions are answered by one for-each-ref, and no reflog."""
    for key in ("PL-7", "PL-8", "PL-9"):
        commit(start(env, key, "t"), f"{key}.txt")  # distinct files: identical commits would share a sha
    log = env["tmp"] / "git.log"
    real = subprocess.run(["which", "git"], capture_output=True, text=True, env=env["env"]).stdout.strip()
    shim = env["tmp"] / "bin" / "git"
    shim.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {real} "$@"\n')
    shim.chmod(0o755)
    hook_context(env)
    verbs = [line.split()[0] for line in log.read_text().splitlines()]
    assert verbs.count("for-each-ref") == 1 and "reflog" not in verbs, log.read_text()
    assert verbs.count("rev-parse") <= 3, log.read_text()  # repository discovery only; was four per task



# --------------------------------------------------------------------------- packaging: guide, install-hook


def _run_setup(*args: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "FLOW_HUMAN")}
    return subprocess.run([sys.executable, "-m", "flow_worktrees", *args], capture_output=True, text=True,
                          env={**env, **extra_env})


def test_guide_prints_the_packaged_guide_outside_any_repo(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-m", "flow_worktrees", "guide"], cwd=tmp_path, capture_output=True,
                            text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("# flow — user guide")


def test_install_hook_adds_once_replaces_script_path_entry_and_keeps_other_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    other = {"type": "command", "command": "echo hi"}
    settings.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [other, {"type": "command", "timeout": 15,
                                                 "command": "python3 ~/.claude/scripts/flow.py status --hook"}]}]}}))
    assert _run_setup("install-hook", "--settings", str(settings)).returncode == 0
    data = json.loads(settings.read_text())
    commands = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert commands == ["echo hi", "flow status --hook"]
    assert data["hooks"]["SessionStart"][0]["hooks"][1]["timeout"] == 15  # what the user set on the entry survives
    assert data["model"] == "x"
    assert (tmp_path / "settings.json.bak").exists()

    again = _run_setup("install-hook", "--settings", str(settings))
    assert "already installed" in again.stdout
    assert json.loads(settings.read_text()) == data


def test_install_hook_creates_a_missing_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "new" / "settings.json"
    assert _run_setup("install-hook", "--settings", str(settings)).returncode == 0
    assert json.loads(settings.read_text()) == {"hooks": {"SessionStart": [
        {"matcher": "startup|clear|compact", "hooks": [{"type": "command", "command": "flow status --hook"}]}]}}
    assert not (tmp_path / "new" / "settings.json.bak").exists()


def test_install_hook_is_human_only_but_print_is_not(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    refused = _run_setup("install-hook", "--settings", str(settings), CLAUDECODE="1")
    assert refused.returncode != 0 and "human-only" in refused.stderr
    assert not settings.exists()
    printed = _run_setup("install-hook", "--print", CLAUDECODE="1")
    assert printed.returncode == 0 and '"flow status --hook"' in printed.stdout


def test_install_hook_leaves_invalid_json_untouched(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    result = _run_setup("install-hook", "--settings", str(settings))
    assert result.returncode != 0 and "not valid JSON" in result.stderr
    assert settings.read_text() == "{not json"


# --------------------------------------------------------------------------- GitHub backend (fake `gh`)

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, re, sys
state_path = os.environ["FAKE_GLAB_STATE"]
state = json.load(open(state_path))
if state.get("offline"):
    sys.stderr.write("error connecting to api.github.com\n"); sys.exit(1)
prs = state.setdefault("prs", {})
args = sys.argv[1:]
def rest(num, pr):
    merged = pr.get("state") == "merged"
    return {"number": int(num), "state": "open" if pr.get("state", "open") == "open" else "closed",
            "merged_at": "2026-09-20T10:00:00Z" if merged else None, "draft": pr.get("draft", False),
            "user": {"login": pr.get("author", "me")}, "head": {"ref": pr["head"], "sha": pr.get("sha", "abc")},
            "html_url": "https://github.example/o/r/pull/" + str(num),
            "created_at": pr.get("created_at", "2026-09-19T09:00:00Z"), "mergeable_state": "unknown"}
if args[0] == "api":
    path = args[1]
    with open(state_path + ".calls", "a") as log:
        log.write(path + "\n")
    if path == "user":
        print(json.dumps({"login": state.get("me", "me")})); sys.exit(0)
    if path == "graphql":
        fields = dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("query="))
        assert fields["owner"] == "{owner}" and fields["repo"] == "{repo}", fields
        pr = prs.get(fields["number"])
        if pr is None:
            print(json.dumps({"data": {"repository": {"pullRequest": None}}})); sys.exit(0)
        print(json.dumps({"data": {"repository": {"pullRequest": {
            "isDraft": pr.get("draft", False), "mergeStateStatus": pr.get("merge", "CLEAN"),
            "commits": {"nodes": [{"commit": {"committedDate": pr.get("pushed_at", "2026-09-19T09:00:00Z")}}]},
            "latestReviews": {"nodes": pr.get("reviews", [])},
            "reviewThreads": {"nodes": pr.get("threads", [])},
            "timelineItems": {"nodes": pr.get("timeline", [])}}}}})); sys.exit(0)
    assert path.startswith("repos/{owner}/{repo}/pulls"), path
    m = re.match(r"repos/\{owner\}/\{repo\}/pulls/(\d+)$", path)
    if m:
        if m.group(1) not in prs:
            sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
        print(json.dumps(rest(m.group(1), prs[m.group(1)]))); sys.exit(0)
    m = re.match(r"repos/\{owner\}/\{repo\}/pulls\?head=\{owner\}:([^&]+)&state=all", path)
    if m:
        from urllib.parse import unquote
        out = [rest(n, pr) for n, pr in sorted(prs.items(), key=lambda kv: -int(kv[0]))
               if pr["head"] == unquote(m.group(1))]
        print(json.dumps(out)); sys.exit(0)
    sys.stderr.write("unknown api path " + path + "\n"); sys.exit(1)
if args[:2] == ["pr", "create"]:
    head, base = args[args.index("--head") + 1], args[args.index("--base") + 1]
    assert "--draft" in args and base == "main", args
    num = str(max([int(n) for n in prs] + [0]) + 1)
    prs[num] = {"head": head, "draft": True}
    json.dump(state, open(state_path, "w"))
    print("https://github.example/o/r/pull/" + num); sys.exit(0)
sys.stderr.write("unsupported: " + " ".join(args) + "\n"); sys.exit(1)
'''


@pytest.fixture()
def gh_env(env: dict) -> dict:
    gh = env["tmp"] / "bin" / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('gate: "true"\nforge: github\n')
    return env


def pr(env: dict, num: str = "5", head: str = "PL-7-smoke", **fields) -> None:
    data = json.loads(env["state"].read_text())
    data.setdefault("prs", {})[num] = {"head": head, **fields}
    env["state"].write_text(json.dumps(data))


def gh_thread(*authors: str, resolved: bool = False, first_id: int = 1) -> dict:
    return {"isResolved": resolved,
            "comments": {"nodes": [{"databaseId": first_id + len(authors) - 1, "author": {"login": authors[-1]}}]}}


def gh_review(who: str, state: str, at: str = "2026-09-20T10:00:00Z", review_id: int = 900) -> dict:
    return {"databaseId": review_id, "state": state, "submittedAt": at, "author": {"login": who}}


def test_github_stage_and_ball_follow_the_pr_when_i_am_the_author(gh_env: dict) -> None:
    start(gh_env)
    pr(gh_env)
    assert stage_ball(gh_env) == ("review-wait", "them")
    assert row(gh_env)["mr"] == "#5:opened"
    pr(gh_env, threads=[gh_thread("rev")])
    assert stage_ball(gh_env) == ("changes", "me")
    pr(gh_env, threads=[gh_thread("rev", "me")])
    assert stage_ball(gh_env) == ("review-wait", "them")
    pr(gh_env, threads=[gh_thread("rev", resolved=True)], reviews=[gh_review("rev", "APPROVED")])
    assert stage_ball(gh_env) == ("merge-wait", "them")
    pr(gh_env, reviews=[gh_review("rev", "APPROVED")], merge="BLOCKED")
    assert stage_ball(gh_env) == ("changes", "me")  # approved, but GitHub will not merge it yet
    pr(gh_env, draft=True)
    assert stage_ball(gh_env) == ("review-wait", "me")  # my draft: I un-draft
    pr(gh_env, state="merged")
    assert stage_ball(gh_env) == ("merged", "me")


def test_github_changes_requested_is_a_thread_the_author_answers_by_pushing(gh_env: dict) -> None:
    start(gh_env)
    cr = [gh_review("rev", "CHANGES_REQUESTED", at="2026-09-20T10:00:00Z")]
    pr(gh_env, reviews=cr, pushed_at="2026-09-19T09:00:00Z")
    assert stage_ball(gh_env) == ("changes", "me")
    pr(gh_env, reviews=cr, pushed_at="2026-09-21T09:00:00Z")
    assert stage_ball(gh_env) == ("review-wait", "them")  # pushed after the verdict: the re-review is owed
    other = {"author": "someone"}
    pr(gh_env, reviews=[gh_review("me", "CHANGES_REQUESTED")], pushed_at="2026-09-19T09:00:00Z", **other)
    assert stage_ball(gh_env) == ("review-wait", "them")  # I requested the changes on their PR


def test_github_next_from_test_creates_a_draft_pr(gh_env: dict) -> None:
    wt = start(gh_env)
    to_test_stage(gh_env, wt)
    push(wt)
    result = flow(gh_env, "next", cwd=wt, check=True)
    assert "created Draft PR #1" in result.stdout and "from #1)" in result.stdout
    assert stage_ball(gh_env) == ("review-wait", "me")
    assert "un-draft #1 (PL-7) in GitHub" in hook_context(gh_env)


def test_github_clean_proves_the_merge_through_the_pr(gh_env: dict) -> None:
    wt = start(gh_env)
    pr(gh_env, "9", state="closed")
    result = flow(gh_env, "clean", "PL-7", human=True)
    assert result.returncode == 2 and "#9 is closed, not merged" in result.stderr
    pr(gh_env, "9", state="merged")
    flow(gh_env, "clean", "PL-7", human=True, check=True)
    assert not wt.exists()


def test_github_stored_prs_fan_out_and_a_missing_one_stays_a_question(gh_env: dict) -> None:
    write_task(gh_env, "PL-1", mr=5)
    write_task(gh_env, "PL-2", mr=6)
    pr(gh_env, "5", head="elsewhere")
    assert row(gh_env, "PL-1")["mr"] == "#5:opened"
    assert row(gh_env, "PL-2")["mr"] == "#6?"


def test_github_log_journals_the_timeline(gh_env: dict) -> None:
    start(gh_env)
    pr(gh_env, timeline=[
        {"__typename": "ReviewRequestedEvent", "id": "E1", "createdAt": "2026-09-20T10:00:00Z",
         "actor": {"login": "me"}, "requestedReviewer": {"login": "rev"}},
        {"__typename": "PullRequestReview", "id": "E2", "submittedAt": "2026-09-20T11:00:00Z",
         "state": "CHANGES_REQUESTED", "body": "rename x", "author": {"login": "rev"}},
        {"__typename": "IssueComment", "id": "E3", "createdAt": "2026-09-20T12:00:00Z", "body": "done",
         "author": {"login": "me"}},
        {"__typename": "LabeledEvent", "id": "E4", "createdAt": "2026-09-20T12:30:00Z"},
    ])
    flow(gh_env, "sync", check=True)
    out = flow(gh_env, "log", "PL-7", check=True).stdout
    assert "#5 me requested review from rev" in out and "#5 rev requested changes: rename x" in out
    assert "#5 me: done" in out and "mr-created" in out and "E4" not in out


def test_gh_offline_warns_and_falls_back_to_the_cache(gh_env: dict) -> None:
    start(gh_env)
    pr(gh_env)
    flow(gh_env, "sync", check=True)
    data = json.loads(gh_env["state"].read_text())
    gh_env["state"].write_text(json.dumps({**data, "offline": True}))
    result = flow(gh_env, "status", check=True)
    assert result.stderr.count("gh offline") == 1 and "from cache" in result.stderr
    assert "#5:opened" in result.stdout


def test_a_cache_written_by_another_forge_is_discarded(gh_env: dict) -> None:
    start(gh_env)
    (gh_env["primary"] / "local-docs" / "flow.local.yml").write_text('gate: "true"\nforge: gitlab\n')
    mr(gh_env)
    data = json.loads(gh_env["state"].read_text())
    gh_env["state"].write_text(json.dumps({**data, "me": "gitlab-user"}))
    flow(gh_env, "sync", check=True)
    (gh_env["primary"] / "local-docs" / "flow.local.yml").write_text('gate: "true"\nforge: github\n')
    gh_env["state"].write_text(json.dumps({**data, "me": "github-user"}))
    flow(gh_env, "sync", check=True)
    cache = json.loads((gh_env["primary"] / "local-docs" / ".flow-cache" / "forge.json").read_text())
    assert cache["forge"] == "github" and cache["mrs"] == {} and cache["by_branch"] == {}
    assert cache["me"] == "github-user"  # the other forge's account says nothing about who I am here
    assert row(gh_env)["mr"] == "-"


@pytest.mark.parametrize("url, host, forge", [
    ("git@github.com:o/r.git", "github.com", "github"),
    ("https://github.com/o/r", "github.com", "github"),
    ("https://token@github.example.com/o/r.git", "github.example.com", "github"),
    ("ssh://git@gitlab.example.com:2222/o/r.git", "gitlab.example.com", "gitlab"),
    ("https://gitlab.flora.ltfs.tools/pub/ai/x.git", "gitlab.flora.ltfs.tools", "gitlab"),
    ("git@git.company.io:o/r.git", "git.company.io", None),
    ("/tmp/origin.git", "", None),
])
def test_forge_is_told_from_origin_host(url: str, host: str, forge: str | None) -> None:
    mod = _flow_module()
    assert mod.url_host(url) == host
    assert mod.forge_for_host(host) == forge


def test_undetectable_forge_fails_only_where_a_forge_is_called(env: dict) -> None:
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('gate: "true"\n')  # local-path origin, no `forge:`
    write_task(env, "PL-1", mr=5)
    assert flow(env, "status", "--offline", check=True).stdout.count("PL-1") == 1
    assert hook_context(env)
    result = flow(env, "sync")
    assert result.returncode != 0 and "set `forge: gitlab` or `forge: github`" in result.stderr
    (env["primary"] / "local-docs" / "flow.local.yml").write_text('forge: bitbucket\n')
    assert "must be one of gitlab, github" in flow(env, "sync").stderr
