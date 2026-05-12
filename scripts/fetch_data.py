#!/usr/bin/env python3
"""Fetches Jira AT QA tickets and Testmo run data, outputs data.json for the automation dashboard."""

import json
import os
import sys
from base64 import b64encode
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── Jira ────────────────────────────────────────────────────────────────────
JIRA_BASE = "https://reigncl.atlassian.net"
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_API_TOKEN"]
_jira_creds = b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_jira_creds}",
    "Accept": "application/json",
}
# Try "Epic Link" first; script auto-retries with parent= if that fails
JIRA_JQL = 'project = EVB AND issuetype = "AT QA" AND "Epic Link" = EVB-32 ORDER BY updated DESC'

# ── Testmo ───────────────────────────────────────────────────────────────────
TESTMO_BASE = "https://applydigital.testmo.net"
TESTMO_TOKEN = os.environ["TESTMO_API_TOKEN"]
TESTMO_HEADERS = {
    "Authorization": f"Bearer {TESTMO_TOKEN}",
    "Accept": "application/json",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def paginate_jira(jql: str, fields: str) -> list:
    items, start = [], 0
    max_results = 100
    while True:
        r = requests.get(
            f"{JIRA_BASE}/rest/api/3/search",
            headers=JIRA_HEADERS,
            params={"jql": jql, "startAt": start, "maxResults": max_results, "fields": fields},
            timeout=30,
        )
        if r.status_code == 400:
            body = r.json()
            if any("Epic Link" in m for m in body.get("errorMessages", [])):
                raise ValueError("JQL_EPIC_LINK_UNSUPPORTED")
            r.raise_for_status()
        r.raise_for_status()
        data = r.json()
        batch = data.get("issues", [])
        items.extend(batch)
        start += len(batch)
        if start >= data.get("total", 0) or not batch:
            break
    return items


def paginate_testmo(endpoint: str, params: dict | None = None) -> list:
    items, page = [], 1
    base_params = params or {}
    while True:
        r = requests.get(
            f"{TESTMO_BASE}{endpoint}",
            headers=TESTMO_HEADERS,
            params={**base_params, "page": page, "per_page": 100},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("data", data) if isinstance(data, dict) else data
        if not batch:
            break
        items.extend(batch)
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        last_page = meta.get("last_page", meta.get("total_pages", 1))
        if page >= last_page or len(batch) < 100:
            break
        page += 1
    return items


# ── Jira ─────────────────────────────────────────────────────────────────────

def fetch_jira_tickets() -> list:
    print("  Fetching Jira tickets…")
    jql = JIRA_JQL
    try:
        raw = paginate_jira(jql, "summary,status,assignee,updated,priority,labels")
    except ValueError as e:
        if str(e) == "JQL_EPIC_LINK_UNSUPPORTED":
            print("  ⚠  'Epic Link' not found — retrying with parent = EVB-32")
            jql = 'project = EVB AND issuetype = "AT QA" AND parent = EVB-32 ORDER BY updated DESC'
            raw = paginate_jira(jql, "summary,status,assignee,updated,priority,labels")
        else:
            raise

    tickets = []
    for issue in raw:
        f = issue["fields"]
        status_obj = f.get("status") or {}
        tickets.append({
            "key": issue["key"],
            "url": f"{JIRA_BASE}/browse/{issue['key']}",
            "summary": f.get("summary", ""),
            "status": status_obj.get("name", "Unknown"),
            "statusCategory": (status_obj.get("statusCategory") or {}).get("key", ""),
            "assignee": ((f.get("assignee") or {}).get("displayName") or "Unassigned"),
            "updated": f.get("updated", ""),
            "priority": ((f.get("priority") or {}).get("name") or ""),
            "labels": f.get("labels", []),
        })
    print(f"  → {len(tickets)} tickets")
    return tickets


# ── Testmo ───────────────────────────────────────────────────────────────────

def fetch_testmo_runs() -> list:
    print("  Fetching Testmo runs…")
    raw = paginate_testmo("/api/v1/runs")
    runs = []
    for r in raw:
        counts = r.get("result_counts") or {}
        runs.append({
            "id": r.get("id"),
            "name": r.get("name", ""),
            "status": r.get("status", ""),
            "is_completed": r.get("is_completed", False),
            "created_at": r.get("created_at", ""),
            "updated_at": r.get("updated_at", ""),
            "passed": r.get("passed_count", 0) or counts.get("passed", 0),
            "failed": r.get("failed_count", 0) or counts.get("failed", 0),
            "pending": r.get("untested_count", 0) or counts.get("untested", 0),
            "total": r.get("count", 0) or r.get("total_count", 0),
        })
    print(f"  → {len(runs)} runs")
    return runs


def fetch_testmo_cases() -> list:
    print("  Fetching Testmo cases…")
    try:
        raw = paginate_testmo("/api/v1/cases", {"expand": "automation_links"})
    except requests.HTTPError as e:
        print(f"  ⚠  Could not fetch cases ({e}) — skipping")
        return []
    cases = []
    for c in raw:
        links = c.get("automation_links") or []
        cases.append({
            "id": c.get("id"),
            "name": c.get("name", ""),
            "is_automated": bool(links),
            "automation_links_count": len(links),
        })
    print(f"  → {len(cases)} cases")
    return cases


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    out_path = os.environ.get("OUTPUT_PATH", "data.json")

    print("Jira:")
    tickets = fetch_jira_tickets()

    print("Testmo:")
    runs = fetch_testmo_runs()
    cases = fetch_testmo_cases()

    status_counts: dict[str, int] = {}
    for t in tickets:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    recent_runs = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)[:10]
    automated = sum(1 for c in cases if c["is_automated"])

    payload = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "jira": {
            "summary": {
                "total": len(tickets),
                "byStatus": status_counts,
            },
            "tickets": tickets,
        },
        "testmo": {
            "summary": {
                "totalRuns": len(runs),
                "totalCases": len(cases),
                "automatedCases": automated,
                "manualCases": len(cases) - automated,
            },
            "recentRuns": recent_runs,
            "cases": cases,
        },
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print(f"\n✓ Written {out_path}")


if __name__ == "__main__":
    main()
