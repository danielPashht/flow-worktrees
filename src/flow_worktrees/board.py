"""flow board — the HTML kanban renderer behind `flow status --html` / `flow board`.

Pure presentation over the row model `cli.status_rows` produces. Kept out of `cli` so the
lifecycle module stays readable; `cli` imports it lazily, so it never loads for a plain `flow status`.
"""

from __future__ import annotations

import datetime as dt
import html
import os

from . import cli as flow

# Board columns: the lifecycle collapsed to four lanes a glance can hold; stage stays visible as a badge.
LANES = [
    ("work", "Work", flow.WORK_STAGES),
    ("review", "Review", ["review-wait", "changes", "merge-wait"]),
    ("done", "Done", ["merged", "cleaned"]),
    ("parked", "Parked", ["parked"]),
]
STALE_WARN, STALE_HOT = 2, 5  # days since `updated` while the ball is mine

HTML_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#1c1e21;--mute:#6b7280;--line:#e3e6ea;
--me:#2563eb;--them:#9ca3af;--warn:#d97706;--hot:#dc2626;--ok:#16a34a;--merged:#7c3aed;--closed:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--card:#1b1e23;--ink:#e6e8eb;--mute:#9aa3ad;--line:#2b3037;
--them:#5b6472}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:baseline;padding:16px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0}header .meta{color:var(--mute)}
.stats{display:flex;gap:8px;flex-wrap:wrap}.stat{padding:2px 10px;border-radius:999px;border:1px solid var(--line);
background:var(--card);cursor:pointer;user-select:none}.stat.on{border-color:var(--me);box-shadow:inset 0 0 0 1px var(--me)}
.stat b{font-variant-numeric:tabular-nums}.stat[data-f]:hover{border-color:var(--me)}
main{display:grid;grid-template-columns:repeat(4,minmax(240px,1fr));gap:16px;padding:16px 20px;overflow-x:auto}
.lane h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 8px}
.lane h2 span{float:right;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--them);border-radius:8px;
padding:10px 12px;margin-bottom:10px;position:relative}
.card.me{border-left-color:var(--me)}.card.cur{outline:2px solid var(--me);outline-offset:1px}
.card.hidden{display:none}
.head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.key{font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.key a{color:inherit;text-decoration:none}
.badge{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mute);white-space:nowrap}
.badge.stage{background:var(--bg)}
.badge.ball-me{border-color:var(--me);color:var(--me)}.badge.ball-them{border-color:var(--them)}
.badge.age-warn{border-color:var(--warn);color:var(--warn)}.badge.age-hot{background:var(--hot);border-color:var(--hot);color:#fff}
.badge.mr-opened{border-color:var(--ok);color:var(--ok)}.badge.mr-draft{border-style:dashed}
.badge.mr-merged{border-color:var(--merged);color:var(--merged)}.badge.mr-closed{border-color:var(--closed);color:var(--closed)}
.badge.mr-unknown{border-color:var(--warn);color:var(--warn)}.badge a{color:inherit;text-decoration:none}
.title{color:var(--mute);margin:4px 0 6px;font-size:13px}
.next{margin:0;white-space:pre-wrap;word-break:break-word}.next::before{content:"→ ";color:var(--mute)}
.blocked{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--mute)}
.chip{padding:0 6px;border-radius:4px;border:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;font-size:11px}
.chip a{color:inherit;text-decoration:none}
.foot{margin-top:8px;font-size:11px;color:var(--mute);display:flex;gap:10px;flex-wrap:wrap;font-family:ui-monospace,Menlo,monospace}
.empty{color:var(--mute);font-size:13px;border:1px dashed var(--line);border-radius:8px;padding:10px;text-align:center}
"""

HTML_JS = """
const q=s=>document.querySelectorAll(s);const F={me:false,them:false,hot:false,active:false};
function apply(){const ball=F.me||F.them;q('.card').forEach(c=>{const d=c.dataset;
const h=(ball&&!((F.me&&d.ball==='me')||(F.them&&d.ball==='them')))||(F.hot&&d.hot!=='1')||(F.active&&d.inactive==='1');
c.classList.toggle('hidden',h)});q('.lane').forEach(l=>{l.querySelector('h2 span').textContent=l.querySelectorAll('.card:not(.hidden)').length})}
q('.stat[data-f]').forEach(b=>b.addEventListener('click',()=>{F[b.dataset.f]=!F[b.dataset.f];b.classList.toggle('on');apply()}));
apply();
"""


def age_class(row: dict) -> str:
    age = row["age"]
    if age is None or row["ball"] != "me" or row["stage"] in flow.INACTIVE:
        return ""
    if age >= STALE_HOT:
        return "age-hot"
    if age >= STALE_WARN:
        return "age-warn"
    return ""


def render_html_card(row: dict, keys: set[str], jira_base: str) -> str:
    e = html.escape
    classes = ["card", f"ball-{row['ball']}" if row["ball"] in ("me", "them") else "ball-none"]
    if row["ball"] == "me":
        classes.append("me")
    if row["current"]:
        classes.append("cur")
    key = e(row["key"])
    key_html = f'<a href="{e(jira_base.rstrip("/"))}/browse/{key}">{key}</a>' if jira_base else key
    ball = {"me": "ball with me", "them": "ball with them"}.get(row["ball"], "no ball")
    badges = [f'<span class="badge stage">{e(row["stage"])}</span>',
              f'<span class="badge ball-{e(row["ball"]) if row["ball"] in ("me", "them") else "none"}">{ball}</span>']
    age_cls = age_class(row)
    badges.append(f'<span class="badge {age_cls}" title="updated {e(row["updated_on"])}">{e(row["updated"])}</span>')
    if row["mr_iid"]:
        cls = f"mr-{e(row['mr_state'])}" + (" mr-draft" if row["mr_draft"] else "")
        text = e(row["mr"])
        text = f'<a href="{e(row["mr_url"])}">{text}</a>' if row["mr_url"] else text
        badges.append(f'<span class="badge {cls}">{text}</span>')
    parts = [f'<article class="{" ".join(classes)}" id="{key}" data-ball="{e(row["ball"])}" '
             f'data-inactive="{1 if row["stage"] in flow.INACTIVE else 0}" data-hot="{1 if age_cls == "age-hot" else 0}">',
             f'<div class="head"><span class="key">{"★ " if row["current"] else ""}{key_html}</span>{"".join(badges)}</div>']
    if row["title"]:
        parts.append(f'<div class="title">{e(row["title"])}</div>')
    parts.append(f'<p class="next">{e(row["next"])}</p>')
    if row["blocked_list"]:
        chips = []
        for item in row["blocked_list"]:
            item = str(item)
            match = flow.KEY_IN_BRANCH.match(item)
            if match and match.group(1) in keys:
                chips.append(f'<span class="chip"><a href="#{e(match.group(1))}">{e(item)}</a></span>')
            else:
                chips.append(f'<span class="chip">{e(item)}</span>')
        parts.append(f'<div class="blocked">blocked on{"".join(chips)}</div>')
    foot = []
    if row["branch_name"]:
        foot.append(f'{e(row["branch_name"])} {e(row["branch_ab"])}'.strip())
    foot.append(f'wt:{e(row["wt"])}')
    parts.append(f'<div class="foot">{"".join(f"<span>{f}</span>" for f in foot)}</div></article>')
    return "".join(parts)


def now_label() -> str:
    """Header timestamp; under FLOW_TODAY the board is reproducible, so no wall-clock minute leaks in."""
    if os.environ.get("FLOW_TODAY"):
        return flow.today()
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def render_html(rows: list[dict], repo_name: str, jira_base: str = "") -> str:
    e = html.escape
    keys = {r["key"] for r in rows}
    mine = sum(r["ball"] == "me" for r in rows)
    theirs = sum(r["ball"] == "them" for r in rows)
    stale = sum(age_class(r) == "age-hot" for r in rows)
    lanes = []
    for lane_id, label, stages in LANES:
        cards = [render_html_card(r, keys, jira_base) for r in rows if r["stage"] in stages]
        body = "".join(cards) if cards else '<div class="empty">empty</div>'
        lanes.append(f'<section class="lane" id="lane-{lane_id}"><h2>{e(label)}<span>{len(cards)}</span></h2>{body}</section>')
    stamp = now_label()
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>flow · {e(repo_name)}</title>"
        f"<style>{HTML_CSS}</style></head><body>"
        f"<header><h1>flow · {e(repo_name)}</h1><span class=\"meta\">{stamp} · {len(rows)} tasks · ★ current branch</span>"
        f"<div class=\"stats\"><span class=\"stat\" data-f=\"me\">ball with me <b>{mine}</b></span>"
        f"<span class=\"stat\" data-f=\"them\">waiting on them <b>{theirs}</b></span>"
        f"<span class=\"stat\" data-f=\"hot\">stale ≥{STALE_HOT}d <b>{stale}</b></span>"
        f"<span class=\"stat\" data-f=\"active\">hide inactive</span></div></header>"
        f"<main>{''.join(lanes)}</main><script>{HTML_JS}</script></body></html>\n"
    )
