from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, redirect, render_template, url_for

from .sample_data import build_sample_service

blueprint = Blueprint("dashboard", __name__)


def _now() -> datetime:
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return datetime.now(timezone)


@blueprint.get("/")
def index() -> str:
    service = build_sample_service()
    return render_template(
        "dashboard.html",
        service=service,
        last_synced=_now(),
    )


@blueprint.post("/sync")
def sync():
    """Refresh the board. Square polling will replace this placeholder later."""
    return redirect(url_for("dashboard.index"), code=303)


@blueprint.get("/healthz")
def healthz() -> tuple[dict[str, str], int]:
    return {"status": "ok"}, 200
