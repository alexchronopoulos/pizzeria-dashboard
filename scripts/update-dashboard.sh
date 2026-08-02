#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PIZZA_DASHBOARD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${PIZZA_DASHBOARD_BRANCH:-master}"
SERVICE="${PIZZA_DASHBOARD_SERVICE:-pizza-dashboard}"
HEALTH_URL="${PIZZA_DASHBOARD_HEALTH_URL:-http://127.0.0.1:8001/}"

cd "$APP_DIR"

# A deployed checkout must never contain edits to tracked files. Runtime files
# such as .env, data/, and .venv/ are ignored and do not trigger this check.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Update aborted: tracked local changes exist in $APP_DIR" >&2
    git status --short >&2
    echo "Resolve or discard these changes before updating." >&2
    exit 1
fi

echo "Pulling origin/$BRANCH..."
git pull --ff-only origin "$BRANCH"

echo "Installing exactly the dependencies in uv.lock..."
uv sync --frozen

echo "Running tests without modifying uv.lock..."
uv run --frozen pytest -q

if command -v systemctl >/dev/null 2>&1 \
    && systemctl list-unit-files "${SERVICE}.service" --no-legend 2>/dev/null \
        | grep -q "${SERVICE}.service"; then
    echo "Restarting ${SERVICE}.service..."
    sudo systemctl restart "$SERVICE"

    if command -v curl >/dev/null 2>&1; then
        echo "Checking dashboard health..."
        sleep 2
        curl --fail --silent --show-error "$HEALTH_URL" >/dev/null
    fi
fi

echo "Dashboard updated to $(git rev-parse --short HEAD)."
