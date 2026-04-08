#!/usr/bin/env python3
"""
LTS Release Dashboard — Flask Web App
======================================
Serves the dashboard as a live website. Data is cached for CACHE_TTL seconds
so Jira/Bitbucket aren't hit on every page load.

Run locally:
    python app.py

Deploy to Azure Web App:
    Set JIRA_EMAIL and JIRA_API_TOKEN in App Service → Configuration → App settings.
    Azure will start the app using the Procfile (gunicorn).

Routes:
    /           → Dashboard (cached)
    /refresh    → Force a fresh pull from Jira + Bitbucket
    /health     → Health check (returns 200 OK)
"""

import os
import sys
import base64
import time
import threading
import requests as http_requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from flask import Flask, Response

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

JIRA_BASE_URL  = "https://leapdev.atlassian.net"
JIRA_PROJECT   = "LTS"
RELEASE_STATUS = "Ready for Live Release"
RELEASE_ALARM  = 5

BLOCKER_STATUSES = [
    "Open", "Investigating", "Development",
    "Under Review", "On Hold", "Waiting on third party",
]

REPO_MAP = {
    "strato-worker":       {"label": "Strato Worker",  "icon": "⚙️"},
    "strato-webapi":       {"label": "WebAPI",          "icon": "🌐"},
    "strato-internal-web": {"label": "Internal Web",    "icon": "🖥️"},
}

IGNORE_REPOS = set()

CACHE_TTL         = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # default 15 minutes
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")          # optional Slack alarm

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

_cache = {
    "html":      None,
    "timestamp": 0,
    "lock":      threading.Lock(),
    "building":  False,   # True while a background build is running
}

LOADING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="4">
<title>LTS Release Dashboard — Loading…</title>
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{--navy:#1E365E;--orange:#F69139;--light-blue:#9DC0E4;--white:#FFFFFF;--bg:#EEEEEE;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Open Sans',sans-serif;background:var(--bg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;min-height:100vh;color:var(--navy);}
.card{background:var(--white);border-radius:12px;padding:48px 56px;text-align:center;
  box-shadow:0 4px 24px rgba(0,0,0,.12);max-width:420px;width:90%;}
.logo{width:52px;height:52px;background:var(--orange);border-radius:10px;display:flex;
  align-items:center;justify-content:center;font-weight:700;color:var(--white);font-size:18px;
  margin:0 auto 20px;}
h1{font-size:20px;font-weight:700;color:var(--navy);margin-bottom:8px;}
p{font-size:13px;color:#666;line-height:1.5;margin-bottom:24px;}
.spinner{width:36px;height:36px;border:3px solid #e0e0e0;border-top-color:var(--orange);
  border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto;}
@keyframes spin{to{transform:rotate(360deg);}}
.note{font-size:11px;color:#aaa;margin-top:20px;}
</style>
</head>
<body>
  <div class="card">
    <div class="logo">LTS</div>
    <h1>Building dashboard…</h1>
    <p>Fetching tickets from Jira and checking Bitbucket commit data.<br>This takes about 30–60 seconds on first load.</p>
    <div class="spinner"></div>
    <div class="note">This page refreshes automatically every 4 seconds.</div>
  </div>
</body>
</html>"""

# Tracks alarm state per repo.
# None = first build not yet done (baseline not established).
# On first build we record the state silently — no Slack fired.
# Slack only fires on a genuine False→True transition in a live session.
_alarm_state: dict = {}   # slug -> bool  (or absent if not yet seen)


def send_slack_alarm(repo_label: str, icon: str, count: int, top_tickets: list):
    """POST a message to Slack when a repo trips the alarm threshold."""
    if not SLACK_WEBHOOK_URL:
        return
    shown        = top_tickets[:5]
    remaining    = count - len(shown)
    ticket_lines = "\n".join(f"• <{JIRA_BASE_URL}/browse/{t}|{t}>" for t in shown)
    if remaining > 0:
        ticket_lines += f"\n_…and {remaining} more_"
    payload = {
        "text": (
            f":rotating_light: *LTS Release Dashboard — {icon} {repo_label}* has "
            f"*{count} ticket{'s' if count != 1 else ''}* waiting for live release.\n"
            f"{ticket_lines}"
        )
    }
    try:
        http_requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"  ⚠  Slack notification failed: {e}")


def check_and_fire_alarms(rel_cols: dict):
    """Fire Slack only on a genuine non-alarming → alarming transition.

    On the very first build after a server start we silently record the
    baseline state without firing — this prevents spurious notifications
    every time Render's free tier wakes the instance back up.
    """
    global _alarm_state
    for slug, cfg in REPO_MAP.items():
        count       = len(rel_cols.get(slug, []))
        is_alarming = count >= RELEASE_ALARM

        if slug not in _alarm_state:
            # First build — establish baseline silently, never fire here
            print(f"  📋 Baseline for {cfg['label']}: {count} tickets "
                  f"({'alarming' if is_alarming else 'ok'})")
            _alarm_state[slug] = is_alarming
            continue

        was_alarming = _alarm_state[slug]
        if is_alarming and not was_alarming:
            # Genuine new alarm during this session — notify
            top_keys = [t["key"] for t in rel_cols[slug][:5]]
            print(f"  🚨 Alarm fired for {cfg['label']} ({count} tickets) — notifying Slack")
            send_slack_alarm(cfg["label"], cfg["icon"], count, top_keys)

        _alarm_state[slug] = is_alarming

# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

def _auth_header():
    creds = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}

def _post_search(jql: str, fields: list, max_results: int = 200) -> list:
    url     = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    headers = {**_auth_header(), "Content-Type": "application/json"}
    body    = {"jql": jql, "maxResults": max_results, "fields": fields}
    r = http_requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    return r.json().get("issues", [])

# ─────────────────────────────────────────────────────────────────────────────
# JIRA QUERIES
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_FIELDS = ["summary", "status", "assignee", "priority", "issuetype", "created", "updated"]

def get_release_tickets():
    return _post_search(
        f'project = {JIRA_PROJECT} AND status = "{RELEASE_STATUS}" ORDER BY updated DESC',
        ISSUE_FIELDS,
    )

def get_blocker_tickets():
    status_jql = " OR ".join(f'status = "{s}"' for s in BLOCKER_STATUSES)
    return _post_search(
        f'project = {JIRA_PROJECT} AND ({status_jql}) ORDER BY updated DESC',
        ISSUE_FIELDS,
    )

def get_repos_for_issue(issue_id: str) -> list:
    url    = f"{JIRA_BASE_URL}/rest/dev-status/1.0/issue/detail"
    params = {"issueId": issue_id, "applicationType": "bitbucket", "dataType": "repository"}
    try:
        r = http_requests.get(url, headers=_auth_header(), params=params, timeout=10)
        if r.status_code in (403, 404):
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ⚠  Dev status failed for {issue_id}: {e}")
        return []

    slugs = []
    for detail in data.get("detail", []):
        for repo in detail.get("repositories", []):
            slug = (repo.get("slug") or repo.get("name", "")).lower().strip()
            if "/" in slug:
                slug = slug.split("/")[-1]
            if slug and slug not in IGNORE_REPOS and slug not in slugs:
                slugs.append(slug)
    return slugs

def fetch_all_repos(issues: list) -> dict:
    result = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(get_repos_for_issue, i["id"]): i["key"] for i in issues}
        for future in as_completed(futures):
            key = futures[future]
            result[key] = future.result()
    return result

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def days_ago(date_str: str) -> int:
    try:
        dt  = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - dt).days
    except Exception:
        return 0

def priority_badge(name: str) -> str:
    n = (name or "").lower()
    cls = ("badge-critical" if n in ("highest", "critical", "blocker")
           else "badge-high"   if n == "high"
           else "badge-medium" if n == "medium"
           else "badge-low")
    return f'<span class="badge {cls}">{name or "—"}</span>'

def age_badge(days: int) -> str:
    cls   = "badge-age old" if days >= 10 else "badge-age stale" if days >= 5 else "badge-age"
    label = f"{days}d" if days > 0 else "today"
    return f'<span class="badge {cls}">{label}</span>'

def status_badge(status_name: str) -> str:
    return f'<span class="badge badge-status">{status_name}</span>'

def initials(name: str) -> str:
    parts = (name or "?").split()
    return (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else (name or "?")[:2].upper()

AVATAR_COLORS = ["#1E365E","#2E4E7E","#0060A9","#58595B","#D97B2B","#162B4D","#3FAF2A","#C02E21"]

def avatar_color(name: str) -> str:
    return AVATAR_COLORS[hash(name) % len(AVATAR_COLORS)]

def ticket_card_html(issue: dict, show_status: bool = False) -> str:
    fields  = issue["fields"]
    key     = issue["key"]
    url     = f"{JIRA_BASE_URL}/browse/{key}"
    summary = fields.get("summary", "(no summary)")[:90]
    if len(fields.get("summary", "")) > 90:
        summary += "…"

    assignee = fields.get("assignee") or {}
    aname    = assignee.get("displayName", "Unassigned")
    acolor   = avatar_color(aname)
    prio     = (fields.get("priority") or {}).get("name", "Medium")
    itype    = (fields.get("issuetype") or {}).get("name", "")
    sname    = fields.get("status", {}).get("name", "")
    days     = days_ago(fields.get("created", ""))

    extra_badge = status_badge(sname) if show_status else f'<span class="badge badge-type">{itype}</span>'

    return f"""
        <div class="ticket" onclick="window.open('{url}','_blank')">
          <div class="ticket-top">
            <a class="ticket-key" href="{url}" target="_blank">{key}</a>
            <div class="ticket-badges">
              {priority_badge(prio)}
              {extra_badge}
            </div>
          </div>
          <div class="ticket-summary">{summary}</div>
          <div class="ticket-meta">
            <div class="ticket-assignee">
              <div class="avatar" style="background:{acolor};">{initials(aname)}</div>
              {aname}
            </div>
            {age_badge(days)}
          </div>
        </div>"""

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

CSS = """
:root {
  --navy:#1E365E;--navy-dark:#162B4D;--navy-mid:#2E4E7E;
  --orange:#F69139;--orange-dark:#D97B2B;--orange-light:#FDAA67;
  --blue:#0060A9;--light-blue:#9DC0E4;--grey:#58595B;
  --text:#444444;--text-muted:#AAB0B8;--bg:#EEEEEE;--bg-alt:#F3F3F3;
  --border:#D8D8D8;--success:#3FAF2A;--danger:#C02E21;--white:#FFFFFF;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Open Sans',sans-serif;background:var(--bg);color:var(--text);font-size:14px;}
.header{background:var(--navy);position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.25);}
.header-accent{height:4px;background:var(--orange);}
.header-inner{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;}
.logo-mark{width:36px;height:36px;background:var(--orange);border-radius:6px;display:flex;
  align-items:center;justify-content:center;font-weight:700;color:var(--white);font-size:15px;}
.header-logo{display:flex;align-items:center;gap:12px;}
.header-title{color:var(--white);font-size:18px;font-weight:600;letter-spacing:-.5px;}
.header-subtitle{color:var(--light-blue);font-size:12px;margin-top:1px;}
.header-meta{display:flex;align-items:center;gap:16px;}
.header-time{text-align:right;color:var(--light-blue);font-size:12px;}
.header-time strong{color:var(--white);}
.refresh-btn{background:rgba(255,255,255,.1);color:var(--white);border:1px solid rgba(255,255,255,.2);
  border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer;text-decoration:none;
  transition:background .15s;}
.refresh-btn:hover{background:rgba(255,255,255,.2);}
.main{padding:24px 28px;max-width:1600px;margin:0 auto;}
.kpi-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px;}
.kpi-card{background:var(--white);border:1px solid var(--border);border-radius:8px;padding:14px 16px;
  border-top:3px solid var(--navy-mid);transition:box-shadow .15s;}
.kpi-card:hover{box-shadow:0 3px 12px rgba(0,0,0,.1);}
.kpi-card.alarm{border-top-color:var(--danger);background:#FFF5F5;}
.kpi-label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;}
.kpi-value{font-size:28px;font-weight:700;color:var(--navy);line-height:1;}
.kpi-card.alarm .kpi-value{color:var(--danger);}
.kpi-sub{font-size:11px;margin-top:4px;}
.kpi-sub.ok{color:var(--success);}
.kpi-sub.warn{color:var(--danger);}
.section{margin-bottom:32px;}
.section-header{display:flex;align-items:center;gap:10px;margin-bottom:14px;}
.section-accent{width:4px;height:22px;border-radius:2px;}
.section-accent.release{background:var(--orange);}
.section-accent.blocker{background:var(--danger);}
.section-title{font-size:17px;font-weight:600;color:var(--navy);letter-spacing:-.3px;}
.section-badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;}
.section-badge.release{background:var(--navy);color:var(--white);}
.release-grid{display:grid;gap:16px;}
.repo-col{background:var(--white);border:1px solid var(--border);border-radius:8px;overflow:hidden;}
.repo-col.alarm{border-color:var(--danger);box-shadow:0 0 0 2px rgba(192,46,33,.15);}
.repo-header{padding:12px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);}
.repo-alarm-strip{height:3px;background:var(--danger);}
.repo-name{font-size:13px;font-weight:600;color:var(--white);}
.repo-count{font-size:12px;font-weight:700;padding:2px 8px;border-radius:10px;
  min-width:26px;text-align:center;background:rgba(255,255,255,.2);color:var(--white);}
.repo-count.has-items{background:var(--orange);}
.repo-count.alarm-items{background:var(--danger);}
.repo-body{padding:10px;}
.repo-empty{padding:20px 10px;text-align:center;color:var(--text-muted);font-size:12px;}
.repo-empty .check{font-size:22px;margin-bottom:6px;}
.ticket{border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:8px;
  background:var(--white);transition:box-shadow .15s,border-color .15s;cursor:pointer;}
.ticket:last-child{margin-bottom:0;}
.ticket:hover{box-shadow:0 2px 8px rgba(0,0,0,.1);border-color:var(--light-blue);}
.ticket-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:6px;}
.ticket-key{font-size:11px;font-weight:600;color:var(--blue);text-decoration:none;white-space:nowrap;}
.ticket-key:hover{text-decoration:underline;}
.ticket-summary{font-size:12px;color:var(--text);line-height:1.4;}
.ticket-meta{display:flex;align-items:center;justify-content:space-between;margin-top:6px;flex-wrap:wrap;gap:4px;}
.ticket-assignee{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--grey);}
.avatar{width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:9px;font-weight:700;color:var(--white);flex-shrink:0;}
.ticket-badges{display:flex;gap:4px;align-items:center;flex-wrap:wrap;}
.badge{font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;white-space:nowrap;}
.badge-critical{background:#FFECEC;color:#7A0000;}
.badge-high{background:#FFECEC;color:var(--danger);}
.badge-medium{background:#FFF4E6;color:var(--orange-dark);}
.badge-low{background:#F0F4F8;color:var(--grey);}
.badge-age{background:var(--bg);color:var(--text-muted);}
.badge-age.stale{background:#FFF4E6;color:var(--orange-dark);}
.badge-age.old{background:#FFECEC;color:var(--danger);}
.badge-type{background:#EFF3F8;color:var(--navy-mid);}
.badge-status{background:#F0F4F8;color:var(--grey);}
.blocker-col .repo-header{background:var(--danger)!important;}
.blocker-col{border-color:#e8c5c5;}
.info-banner{background:#EFF7FF;border:1px solid var(--light-blue);border-left:4px solid var(--blue);
  border-radius:6px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:var(--text);}
.footer{background:var(--navy-dark);color:var(--light-blue);text-align:center;padding:14px;font-size:11px;margin-top:32px;}
.footer strong{color:var(--white);}
@media(max-width:900px){.release-grid{grid-template-columns:1fr!important;}.kpi-strip{grid-template-columns:repeat(2,1fr);}}
"""

REPO_HEADER_COLORS = {
    "strato-worker":       "#1E365E",
    "strato-webapi":       "#2E4E7E",
    "strato-internal-web": "#0060A9",
    "_undetected":         "#58595B",
}

# ─────────────────────────────────────────────────────────────────────────────
# HTML BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def build_column_html(slug, label, icon, tickets, show_status=False, extra_class=""):
    color     = REPO_HEADER_COLORS.get(slug, "#58595B")
    count     = len(tickets)
    alarm     = count >= RELEASE_ALARM and not show_status
    col_class = "alarm" if alarm else ""
    cnt_class = "alarm-items" if alarm else ("has-items" if count > 0 else "")

    if count == 0:
        body = '<div class="repo-empty"><div class="check">✅</div><div>Nothing here</div></div>'
    else:
        body = "".join(ticket_card_html(t, show_status=show_status) for t in tickets)

    alarm_strip = '<div class="repo-alarm-strip"></div>' if alarm else ""

    return f"""
      <div class="repo-col {col_class} {extra_class}">
        {alarm_strip}
        <div class="repo-header" style="background:{color};">
          <div class="repo-name">{icon} {label}{"  🚨" if alarm else ""}</div>
          <div class="repo-count {cnt_class}">{count}</div>
        </div>
        <div class="repo-body">{body}</div>
      </div>"""

def organise_by_repo(issues, ticket_repos):
    repo_columns  = {slug: [] for slug in REPO_MAP}
    undetected    = []
    unknown_slugs = []
    for issue in issues:
        key   = issue["key"]
        slugs = ticket_repos.get(key, [])
        known   = [s for s in slugs if s in REPO_MAP]
        unknown = [s for s in slugs if s not in REPO_MAP and s not in IGNORE_REPOS]
        for u in unknown:
            if u not in unknown_slugs:
                unknown_slugs.append(u)
        if not known:
            undetected.append(issue)
        else:
            for s in known:
                repo_columns[s].append(issue)
    return repo_columns, undetected, unknown_slugs

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD GENERATOR  (returns HTML string)
# ─────────────────────────────────────────────────────────────────────────────

def build_dashboard() -> str:
    now_str = datetime.now().strftime("%d %B %Y, %H:%M AEST").lstrip("0")

    print(f"[{now_str}] Building dashboard...")
    release_issues = get_release_tickets()
    blocker_issues = get_blocker_tickets()
    all_issues     = release_issues + blocker_issues
    ticket_repos   = fetch_all_repos(all_issues)

    rel_cols, rel_undetected, rel_unknown = organise_by_repo(release_issues, ticket_repos)
    blk_cols, _,              blk_unknown = organise_by_repo(blocker_issues, ticket_repos)

    check_and_fire_alarms(rel_cols)

    # KPI strip
    kpi_html = ""
    for slug, cfg in REPO_MAP.items():
        count = len(rel_cols.get(slug, []))
        alarm = count >= RELEASE_ALARM
        kpi_html += f"""
        <div class="kpi-card {'alarm' if alarm else ''}">
          <div class="kpi-label">{cfg['icon']} {cfg['label']}</div>
          <div class="kpi-value">{count}</div>
          <div class="kpi-sub {'warn' if alarm else 'ok'}">
            {'🚨 Release overdue' if alarm else '✅ Within threshold'}
          </div>
        </div>"""

    # Release columns
    num_cols      = len(REPO_MAP) + 1
    grid_cols     = f"repeat({num_cols}, 1fr)"
    blk_grid_cols = f"repeat({len(REPO_MAP)}, 1fr)"

    rel_col_htmls = [
        build_column_html(slug, cfg["label"], cfg["icon"], rel_cols.get(slug, []))
        for slug, cfg in REPO_MAP.items()
    ]
    rel_col_htmls.append(
        build_column_html("_undetected", "No Repo Detected", "❓", rel_undetected)
    )

    # Blocker columns (no undetected column)
    blk_col_htmls = [
        build_column_html(slug, cfg["label"], cfg["icon"],
                          blk_cols.get(slug, []), show_status=True, extra_class="blocker-col")
        for slug, cfg in REPO_MAP.items()
    ]

    # Unknown slug banner
    all_unknown    = list({s for s in rel_unknown + blk_unknown})
    unknown_banner = ""
    if all_unknown:
        slug_list      = ", ".join(f"<code>{s}</code>" for s in all_unknown)
        unknown_banner = f'<div class="info-banner">ℹ️ <strong>Unknown repo slugs:</strong> {slug_list} — add to REPO_MAP in app.py</div>'

    total_release = len(release_issues)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="{CACHE_TTL}">
<title>LTS Release Dashboard — LEAP Strato</title>
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>

<div class="header">
  <div class="header-accent"></div>
  <div class="header-inner">
    <div class="header-logo">
      <div class="logo-mark">LTS</div>
      <div>
        <div class="header-title">Release Dashboard</div>
        <div class="header-subtitle">LEAP Strato · leapdev.atlassian.net</div>
      </div>
    </div>
    <div class="header-meta">
      <div class="header-time">
        <div><strong>Last updated</strong></div>
        <div>{now_str}</div>
        <div style="margin-top:2px;color:#9DC0E4;font-size:11px;">Auto-refreshes every {CACHE_TTL // 60} min</div>
      </div>
      <a class="refresh-btn" href="/refresh">↻ Refresh now</a>
    </div>
  </div>
</div>

<div class="main">

  <div class="kpi-strip">{kpi_html}</div>

  {unknown_banner}

  <div class="section">
    <div class="section-header">
      <div class="section-accent release"></div>
      <div class="section-title">Ready for Live Release</div>
      <span class="section-badge release">{total_release} ticket{'s' if total_release != 1 else ''}</span>
    </div>
    <div class="release-grid" style="grid-template-columns:{grid_cols};">
      {"".join(rel_col_htmls)}
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      <div class="section-accent blocker"></div>
      <div class="section-title">Release Blockers — commits in test, not yet ready</div>
    </div>
    <div class="release-grid" style="grid-template-columns:{blk_grid_cols};">
      {"".join(blk_col_htmls)}
    </div>
  </div>

</div>

<div class="footer">
  <strong>LEAP Strato (LTS)</strong> · leapdev.atlassian.net ·
  Generated {now_str} · Repo detection via Bitbucket dev status API
</div>

</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "OK", 200

def _background_build():
    """Run build_dashboard() in a background thread; store result in cache."""
    try:
        html = build_dashboard()
        with _cache["lock"]:
            _cache["html"]      = html
            _cache["timestamp"] = time.time()
            _cache["building"]  = False
        print("  ✅ Background build complete.")
    except Exception as e:
        print(f"  ❌ Background build failed: {e}")
        with _cache["lock"]:
            _cache["building"] = False


def _trigger_build_if_needed():
    """Start a background build if cache is stale and no build is running."""
    with _cache["lock"]:
        age   = time.time() - _cache["timestamp"]
        stale = _cache["html"] is None or age > CACHE_TTL
        if stale and not _cache["building"]:
            _cache["building"] = True
            t = threading.Thread(target=_background_build, daemon=True)
            t.start()
            return True   # build just kicked off
    return False


@app.route("/refresh")
def refresh():
    """Force a fresh build; show loading page until it's done."""
    with _cache["lock"]:
        _cache["html"]      = None   # invalidate cache
        _cache["timestamp"] = 0
        _cache["building"]  = False  # allow _trigger_build_if_needed to start one
    _trigger_build_if_needed()
    return Response(
        '<meta http-equiv="refresh" content="2; url=/">Refreshing…',
        mimetype="text/html",
    )


@app.route("/")
def dashboard():
    _trigger_build_if_needed()

    with _cache["lock"]:
        html = _cache["html"]

    if html is None:
        return Response(LOADING_PAGE, mimetype="text/html")

    return Response(html, mimetype="text/html")

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        print("ERROR: Set JIRA_EMAIL and JIRA_API_TOKEN in your .env file.")
        sys.exit(1)
    port = int(os.getenv("PORT", 5000))
    print(f"Starting LTS Dashboard on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
