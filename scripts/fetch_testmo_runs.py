#!/usr/bin/env python3
"""Fetches Testmo manual-run status counts (passed/failed/retest/blocked/untested)
and writes testmo/data.json for the Testmo Runs dashboard.

Scope resolution (first non-empty wins):
  1. TESTMO_RUN_IDS   – explicit comma-separated list of run IDs
  2. TESTMO_GROUP_ID  – filter project runs whose `group_id` field matches
  3. TESTMO_MILESTONE_ID – list runs from that milestone

The Testmo REST API exposes per-status counts as status1_count..status24_count
on the GET /api/v1/runs/{id} response. Defaults (system statuses):
  1 Untested · 2 Passed · 3 Failed · 4 Retest · 5 Blocked · 6 Skipped
"""

import json
import os
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


def _required_env(name: str) -> str:
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
        print(f"ERROR: {name} still has a placeholder value ({v!r}).", file=sys.stderr)
        sys.exit(2)
    return v


TESTMO_BASE = os.environ.get("TESTMO_BASE_URL", "https://applydigital.testmo.net").rstrip("/")
TESTMO_TOKEN = _required_env("TESTMO_API_TOKEN")
TESTMO_PROJECT_ID = os.environ.get("TESTMO_PROJECT_ID", "44")
TESTMO_RUN_IDS = os.environ.get("TESTMO_RUN_IDS", "").strip()
TESTMO_GROUP_ID = os.environ.get("TESTMO_GROUP_ID", "").strip()
TESTMO_MILESTONE_ID = os.environ.get("TESTMO_MILESTONE_ID", "").strip()

HEADERS = {
    "Authorization": f"Bearer {TESTMO_TOKEN}",
    "Accept": "application/json",
}

# System status id → label. Custom statuses (7..24) fall through to "Status N".
STATUS_LABELS = {
    1: "Untested",
    2: "Passed",
    3: "Failed",
    4: "Retest",
    5: "Blocked",
    6: "Skipped",
}


def paginate(endpoint: str, params: dict | None = None) -> list:
    """Testmo wraps list responses in {'result': [...], 'page', 'last_page', ...}."""
    items: list = []
    page = 1
    base_params = params or {}
    while True:
        r = requests.get(
            f"{TESTMO_BASE}{endpoint}",
            headers=HEADERS,
            params={**base_params, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("result", []) if isinstance(data, dict) else (data or [])
        items.extend(batch)
        last_page = data.get("last_page", 1) if isinstance(data, dict) else 1
        # Testmo returns last_page=None when the total is 0.
        if not batch or last_page is None or page >= last_page:
            break
        page += 1
    return items


def get_run(run_id: int | str) -> dict:
    r = requests.get(f"{TESTMO_BASE}/api/v1/runs/{run_id}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("result", data) if isinstance(data, dict) else data


def list_project_runs() -> list:
    params = {}
    if TESTMO_MILESTONE_ID:
        params["milestone_id"] = TESTMO_MILESTONE_ID
    return paginate(f"/api/v1/projects/{TESTMO_PROJECT_ID}/runs", params or None)


def list_run_results(run_id: int | str) -> list[dict]:
    """Results = status-change events on tests in the run. Each test can have
    multiple result rows; only the one with is_latest=true reflects current status.
    Returns the latest result per test (deduped client-side)."""
    raw = paginate(f"/api/v1/runs/{run_id}/results")
    by_test: dict[int, dict] = {}
    for r in raw:
        if not r.get("is_latest"):
            continue
        tid = r.get("test_id")
        if tid is None:
            continue
        by_test[tid] = r
    return list(by_test.values())


_CASE_NAME_CACHE: dict[int, str] | None = None

def case_name_lookup() -> dict[int, str]:
    """Fetches project cases once and caches a {case_id: name} map."""
    global _CASE_NAME_CACHE
    if _CASE_NAME_CACHE is not None:
        return _CASE_NAME_CACHE
    print(f"  Fetching case name lookup (project {TESTMO_PROJECT_ID})…")
    raw = paginate(f"/api/v1/projects/{TESTMO_PROJECT_ID}/cases")
    _CASE_NAME_CACHE = {c["id"]: c.get("name", "") for c in raw if "id" in c}
    print(f"  → {len(_CASE_NAME_CACHE)} cases cached")
    return _CASE_NAME_CACHE


def resolve_run_ids() -> list[int]:
    """Decides which runs to pull based on env scope."""
    if TESTMO_RUN_IDS:
        ids = [int(x) for x in TESTMO_RUN_IDS.split(",") if x.strip().isdigit()]
        print(f"  Scope: explicit run IDs → {ids}")
        return ids

    print(f"  Scope: listing project {TESTMO_PROJECT_ID} runs"
          + (f" filtered by milestone {TESTMO_MILESTONE_ID}" if TESTMO_MILESTONE_ID else "")
          + (f", group_id {TESTMO_GROUP_ID}" if TESTMO_GROUP_ID else ""))
    raw = list_project_runs()
    if TESTMO_GROUP_ID:
        gid = int(TESTMO_GROUP_ID)
        raw = [r for r in raw if r.get("group_id") == gid]
    ids = [r["id"] for r in raw if r.get("id") is not None]
    print(f"  → {len(ids)} runs in scope")
    return ids


def normalize_run(run: dict) -> dict:
    """Pulls the per-status counts off the run payload into a flat shape."""
    status_counts = {}
    for k, v in run.items():
        if not k.startswith("status") or not k.endswith("_count"):
            continue
        mid = k[len("status"):-len("_count")]
        if not mid.isdigit():
            continue
        sid = int(mid)
        label = STATUS_LABELS.get(sid, f"Status {sid}")
        status_counts[label] = {"id": sid, "count": int(v or 0)}

    # `untested_count` is exposed as a top-level field (mirrors status1_count).
    if "Untested" not in status_counts and "untested_count" in run:
        status_counts["Untested"] = {"id": 1, "count": int(run.get("untested_count") or 0)}

    total = int(run.get("total_count") or 0) or sum(s["count"] for s in status_counts.values())
    completed = int(run.get("completed_count") or 0)
    remaining = total - completed

    return {
        "id": run.get("id"),
        "name": run.get("name", ""),
        "milestone_id": run.get("milestone_id"),
        "group_id": run.get("group_id"),
        "project_id": run.get("project_id"),
        "is_closed": bool(run.get("is_closed", False)),
        "created_at": run.get("created_at", ""),
        "updated_at": run.get("updated_at", ""),
        "closed_at": run.get("closed_at", ""),
        "elapsed_ms": run.get("elapsed"),
        "total": total,
        "completed": completed,
        "remaining": remaining,
        "passed": status_counts.get("Passed", {}).get("count", 0),
        "failed": status_counts.get("Failed", {}).get("count", 0),
        "retest": status_counts.get("Retest", {}).get("count", 0),
        "blocked": status_counts.get("Blocked", {}).get("count", 0),
        "untested": status_counts.get("Untested", {}).get("count", 0),
        "skipped": status_counts.get("Skipped", {}).get("count", 0),
        "statusCounts": status_counts,
        "url": f"{TESTMO_BASE}/runs/view/{run.get('id')}"
               + (f"?group_id={run.get('group_id')}" if run.get("group_id") else ""),
    }


def fetch_run_cases(run_id: int | str) -> list[dict]:
    """Returns one row per executed test in the run: id, name, status, etc.
    Tests that have never been touched don't appear (Testmo doesn't create a
    result row until someone records a status)."""
    print(f"    Fetching results for run {run_id}…")
    results = list_run_results(run_id)
    if not results:
        print(f"    → 0 executed tests (table empty until QA records results)")
        return []
    names = case_name_lookup()
    rows = []
    for r in results:
        sid = r.get("status_id")
        rows.append({
            "caseId": r.get("case_id"),
            "testId": r.get("test_id"),
            "name": names.get(r.get("case_id"), f"Case #{r.get('case_id')}"),
            "statusId": sid,
            "status": STATUS_LABELS.get(sid, f"Status {sid}"),
            "note": r.get("note") or "",
            "assigneeId": r.get("assignee_id"),
            "updatedAt": r.get("created_at"),
        })
    rows.sort(key=lambda x: (x["statusId"] or 999, x["name"].lower()))
    print(f"    → {len(rows)} executed tests")
    return rows


def main():
    out_path = os.environ.get("OUTPUT_PATH", "testmo/data.json")

    print("Testmo runs:")
    run_ids = resolve_run_ids()
    if not run_ids:
        print("  ⚠  No runs matched the configured scope — writing empty payload.")

    runs = []
    for rid in run_ids:
        try:
            print(f"  Fetching run {rid}…")
            run = normalize_run(get_run(rid))
            run["cases"] = fetch_run_cases(rid)
            runs.append(run)
        except requests.HTTPError as e:
            print(f"  ⚠  Run {rid}: {e} — skipping")

    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    payload = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "baseUrl": TESTMO_BASE,
            "projectId": TESTMO_PROJECT_ID,
            "runIds": TESTMO_RUN_IDS or None,
            "groupId": TESTMO_GROUP_ID or None,
            "milestoneId": TESTMO_MILESTONE_ID or None,
        },
        "runs": runs,
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print(f"\n✓ Written {out_path} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
