#!/usr/bin/env python3
"""Fetches Jira AT QA tickets and Testmo run data, outputs data.json for the automation dashboard."""

import json
import os
import re
import sys
from base64 import b64encode
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


def _required_env(name: str) -> str:
    """Fail fast with a clear message when a required secret is empty or still a placeholder.

    In GitHub Actions, `${{ secrets.X }}` resolves to an empty string when the secret
    is missing or stored under the wrong tab (Variables vs Secrets).
    """
    v = (os.environ.get(name) or "").strip()
    if not v:
        print(
            f"ERROR: {name} is empty. In GitHub: Settings → Secrets and variables → Actions → Secrets "
            f"(not Variables). Locally: check .env.",
            file=sys.stderr,
        )
        sys.exit(2)
    low = v.lower()
    if low.startswith("your_") or low.endswith("_here") or "placeholder" in low:
        print(f"ERROR: {name} still has a placeholder value ({v!r}). Replace it with a real token.",
              file=sys.stderr)
        sys.exit(2)
    return v


# ── Jira ────────────────────────────────────────────────────────────────────
JIRA_BASE = "https://e2x.atlassian.net"
JIRA_EMAIL = _required_env("JIRA_EMAIL")
JIRA_TOKEN = _required_env("JIRA_API_TOKEN")
_jira_creds = b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_jira_creds}",
    "Accept": "application/json",
}
# Try "Epic Link" first; script auto-retries with parent= if that fails
JIRA_JQL = 'project = WSC AND issuetype = "AT QA" ORDER BY updated DESC'

# ── Testmo ───────────────────────────────────────────────────────────────────
TESTMO_BASE = "https://applydigital.testmo.net"
TESTMO_TOKEN = _required_env("TESTMO_API_TOKEN")
TESTMO_PROJECT_ID = os.environ.get("TESTMO_PROJECT_ID", "44")
# Automation source to scope runs to. Set "" to use manual runs endpoint instead.
TESTMO_AUTOMATION_SOURCE_ID = os.environ.get("TESTMO_AUTOMATION_SOURCE_ID", "")
# Regex applied to run name (case-insensitive).
TESTMO_RUN_NAME_PATTERN = os.environ.get("TESTMO_RUN_NAME_PATTERN", r"^Regression")
# Root folder for case-coverage scope. Set "" to disable filter.
TESTMO_SCOPE_FOLDER_ID = os.environ.get("TESTMO_SCOPE_FOLDER_ID", "20991")
TESTMO_HEADERS = {
    "Authorization": f"Bearer {TESTMO_TOKEN}",
    "Accept": "application/json",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def paginate_jira(jql: str, fields: str) -> list:
    """Uses the new /rest/api/3/search/jql endpoint (token-paginated, no total).
    The legacy /rest/api/3/search returns 410 Gone as of 2025."""
    items: list = []
    next_token: str | None = None
    while True:
        params: dict = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_token:
            params["nextPageToken"] = next_token
        r = requests.get(
            f"{JIRA_BASE}/rest/api/3/search/jql",
            headers=JIRA_HEADERS,
            params=params,
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
        if data.get("isLast") or not batch:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return items


def paginate_testmo(endpoint: str, params: dict | None = None) -> list:
    """Testmo wraps list responses in {'result': [...], 'page', 'last_page', ...}.
    Default page size is 100; sending per_page=N fails for most N, so we don't set it."""
    items: list = []
    page = 1
    base_params = params or {}
    while True:
        r = requests.get(
            f"{TESTMO_BASE}{endpoint}",
            headers=TESTMO_HEADERS,
            params={**base_params, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("result", []) if isinstance(data, dict) else (data or [])
        items.extend(batch)
        last_page = data.get("last_page", 1) if isinstance(data, dict) else 1
        if page >= last_page or not batch:
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
            print("  ⚠  'Epic Link' not found — retrying without epic filter")
            jql = 'project = WSC AND issuetype = "AT QA" ORDER BY updated DESC'
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
    """Fetches runs from project and filters to the scope of interest.

    Uses automation/runs endpoint when TESTMO_AUTOMATION_SOURCE_ID is set,
    otherwise falls back to manual runs endpoint (for projects like Whitestuff
    that don't use automation sources).
    """
    src = TESTMO_AUTOMATION_SOURCE_ID
    name_re = re.compile(TESTMO_RUN_NAME_PATTERN, re.IGNORECASE) if TESTMO_RUN_NAME_PATTERN else None

    if src:
        endpoint = f"/api/v1/projects/{TESTMO_PROJECT_ID}/automation/runs"
        print(f"  Fetching Testmo automation runs (project {TESTMO_PROJECT_ID}, "
              f"source={src}, name~={TESTMO_RUN_NAME_PATTERN or '*'})…")
    else:
        endpoint = f"/api/v1/projects/{TESTMO_PROJECT_ID}/runs"
        print(f"  Fetching Testmo manual runs (project {TESTMO_PROJECT_ID}, "
              f"name~={TESTMO_RUN_NAME_PATTERN or '*'})…")

    raw = paginate_testmo(endpoint)

    runs = []
    for r in raw:
        if src and str(r.get("source_id")) != src:
            continue
        if name_re and not name_re.search(r.get("name", "")):
            continue
        runs.append({
            "id": r.get("id"),
            "name": r.get("name", ""),
            "source_id": r.get("source_id"),
            "milestone_id": r.get("milestone_id"),
            "config_id": r.get("config_id"),
            "status": r.get("status"),
            "is_completed": r.get("is_closed", r.get("is_completed", False)),
            "elapsed_ms": r.get("elapsed"),
            "created_at": r.get("created_at", ""),
            "completed_at": r.get("closed_at", r.get("completed_at", "")),
            "passed": r.get("success_count", 0),
            "failed": r.get("failure_count", 0),
            "pending": r.get("untested_count", 0),
            "completed": r.get("completed_count", 0),
            "total": r.get("total_count", 0),
        })
    print(f"  → {len(runs)} runs (filtered from {len(raw)} total)")
    return runs


def fetch_testmo_folders() -> list:
    """Returns the full folder list for the project (used to build path/scope filter)."""
    print(f"  Fetching Testmo folders…")
    raw = paginate_testmo(f"/api/v1/projects/{TESTMO_PROJECT_ID}/folders")
    print(f"  → {len(raw)} folders")
    return raw


def build_folder_lookups(folders: list) -> tuple[dict, dict, dict]:
    """Returns (by_id, kids, path_strings). Path skips the root for brevity."""
    by_id = {f["id"]: f for f in folders}
    kids: dict[int | None, list[int]] = {}
    for f in folders:
        kids.setdefault(f.get("parent_id"), []).append(f["id"])

    paths: dict[int, str] = {}
    for fid in by_id:
        parts = []
        cur = fid
        # depth guard in case of cycles
        for _ in range(20):
            if cur is None:
                break
            node = by_id.get(cur)
            if not node:
                break
            parts.append(node["name"])
            cur = node.get("parent_id")
        paths[fid] = " > ".join(reversed(parts))
    return by_id, kids, paths


def descendants_of(root_id: int, kids: dict) -> set[int]:
    out, stack = {root_id}, [root_id]
    while stack:
        n = stack.pop()
        for c in kids.get(n, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def fetch_testmo_cases() -> list:
    """Fetches all project cases. The caller filters/categorizes by folder."""
    print(f"  Fetching Testmo cases (project {TESTMO_PROJECT_ID})…")
    try:
        raw = paginate_testmo(f"/api/v1/projects/{TESTMO_PROJECT_ID}/cases")
    except requests.HTTPError as e:
        print(f"  ⚠  Could not fetch cases ({e}) — skipping")
        return []
    print(f"  → {len(raw)} cases")
    return raw


def coverage_bucket(case: dict) -> str:
    """Maps a case to an automation-coverage bucket using custom_automated as source of truth.

    Testmo's `has_automation` flag isn't populated in this project even when CI runs exist,
    so the QA-curated `custom_automated` field is the only reliable signal:
      1 → automated, 0 → not automated, null → unclassified (needs triage).
    """
    v = case.get("custom_automated")
    if v == 1:
        return "automated"
    if v == 0:
        return "notAutomated"
    return "unclassified"


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    out_path = os.environ.get("OUTPUT_PATH", "data.json")

    print("Jira:")
    tickets = fetch_jira_tickets()

    print("Testmo:")
    folders = fetch_testmo_folders()
    by_id, kids, paths = build_folder_lookups(folders)

    scope_root = int(TESTMO_SCOPE_FOLDER_ID) if TESTMO_SCOPE_FOLDER_ID else None
    in_scope: set[int] | None = descendants_of(scope_root, kids) if scope_root else None
    scope_name = paths.get(scope_root, "") if scope_root else "(no filter)"

    all_cases = fetch_testmo_cases()
    if in_scope is None:
        cases = all_cases
    else:
        cases = [c for c in all_cases if c.get("folder_id") in in_scope]
        print(f"  → {len(cases)} cases in scope ‘{scope_name}’ (of {len(all_cases)} project-wide)")

    runs = fetch_testmo_runs()

    # ── Jira summary ─────────────────────────────────────────────────────────
    status_counts: dict[str, int] = {}
    for t in tickets:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    # ── Coverage buckets ─────────────────────────────────────────────────────
    bucket_counts = {"automated": 0, "notAutomated": 0, "unclassified": 0}
    folder_stats: dict[int, dict] = {}
    for c in cases:
        b = coverage_bucket(c)
        bucket_counts[b] += 1
        fid = c.get("folder_id")
        if fid is None:
            continue
        fs = folder_stats.setdefault(fid, {"automated": 0, "notAutomated": 0, "unclassified": 0, "total": 0})
        fs[b] += 1
        fs["total"] += 1

    # Roll up to feature-level folders. Each immediate child of scope_root
    # (e.g. "Test Funcionales", "Test No Funcionales") tends to be a wrapper —
    # descend one extra level so we surface real feature folders (Login, Checkout…).
    coverage_by_folder = []
    if scope_root:
        feature_ids: list[tuple[int, int]] = []  # (folder_id, group_id_for_grouping)
        for child_id in kids.get(scope_root, []):
            grandchildren = kids.get(child_id, [])
            if grandchildren:
                for gid in grandchildren:
                    feature_ids.append((gid, child_id))
            else:
                feature_ids.append((child_id, child_id))
        for fid, group_id in feature_ids:
            sub = descendants_of(fid, kids)
            agg = {"automated": 0, "notAutomated": 0, "unclassified": 0, "total": 0}
            for sid in sub:
                fs = folder_stats.get(sid)
                if not fs:
                    continue
                for k in agg:
                    agg[k] += fs[k]
            if agg["total"]:
                coverage_by_folder.append({
                    "folderId": fid,
                    "name": by_id[fid]["name"],
                    "group": by_id[group_id]["name"],
                    **agg,
                })
        coverage_by_folder.sort(key=lambda x: (x["group"], -x["total"]))

    # ── Runs summary ─────────────────────────────────────────────────────────
    sorted_runs = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)
    recent_runs = sorted_runs[:10]
    latest = sorted_runs[0] if sorted_runs else {}
    total_passed = sum(r["passed"] for r in runs)
    total_failed = sum(r["failed"] for r in runs)
    total_executed = total_passed + total_failed
    pass_rate = (total_passed / total_executed * 100) if total_executed else 0.0

    # ── Slim case list for the payload (drop verbose custom_steps/description) ──
    slim_cases = [
        {
            "id": c.get("id"),
            "name": c.get("name", ""),
            "folderId": c.get("folder_id"),
            "folderPath": paths.get(c.get("folder_id"), ""),
            "bucket": coverage_bucket(c),
            "priority": c.get("custom_priority"),
        }
        for c in cases
    ]

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
            "scope": {
                "projectId": TESTMO_PROJECT_ID,
                "automationSourceId": TESTMO_AUTOMATION_SOURCE_ID,
                "runNamePattern": TESTMO_RUN_NAME_PATTERN,
                "scopeFolderId": TESTMO_SCOPE_FOLDER_ID,
                "scopeFolderName": scope_name,
            },
            "coverage": {
                "total": len(cases),
                **bucket_counts,
                "automatedPct": round(bucket_counts["automated"] / len(cases) * 100, 1) if cases else 0.0,
                "byFolder": coverage_by_folder,
            },
            "runs": {
                "total": len(runs),
                "latestPassed": latest.get("passed", 0),
                "latestFailed": latest.get("failed", 0),
                "latestPending": latest.get("pending", 0),
                "latestTotal": latest.get("total", 0),
                "latestRunName": latest.get("name", ""),
                "latestRunDate": latest.get("created_at", ""),
                "passRate": round(pass_rate, 1),
                "recent": recent_runs,
            },
            "cases": slim_cases,
        },
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print(f"\n✓ Written {out_path}")


if __name__ == "__main__":
    main()
