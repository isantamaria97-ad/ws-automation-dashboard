#!/usr/bin/env bash
set -e

ENV_FILE="$(dirname "$0")/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "❌  .env not found. Copy .env.example and fill in your tokens:"
  echo "    cp .env.example .env"
  exit 1
fi

# Load .env
set -a; source "$ENV_FILE"; set +a

PORT=${PORT:-8080}
export TESTMO_PROJECT_ID="${TESTMO_PROJECT_ID:-9}"
export TESTMO_AUTOMATION_SOURCE_ID="${TESTMO_AUTOMATION_SOURCE_ID:-17}"
export TESTMO_RUN_NAME_PATTERN="${TESTMO_RUN_NAME_PATTERN:-^CL.*B2B Web}"
export TESTMO_SCOPE_FOLDER_ID="${TESTMO_SCOPE_FOLDER_ID:-717}"

echo "▶  Fetching data from Jira + Testmo (project ${TESTMO_PROJECT_ID}, folder ${TESTMO_SCOPE_FOLDER_ID}, source ${TESTMO_AUTOMATION_SOURCE_ID}, name ~ ${TESTMO_RUN_NAME_PATTERN})…"
if ! python3 -c "import requests" 2>/dev/null; then
  echo "   installing 'requests'…"
  python3 -m pip install --user --quiet requests
fi
OUTPUT_PATH=data.json python3 scripts/fetch_data.py

echo ""
echo "▶  Serving dashboard at http://localhost:${PORT}"
echo "   Press Ctrl+C to stop."
echo ""
open "http://localhost:${PORT}" 2>/dev/null || true
python3 -m http.server "$PORT"
