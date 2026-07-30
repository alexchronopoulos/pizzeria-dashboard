from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from .sample_data import build_sample_service
from .service_state import (
    build_inventory_summary,
    load_state,
    save_state,
    state_from_form,
)

blueprint = Blueprint("dashboard", __name__)


def _now() -> datetime:
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return datetime.now(timezone)


def _state_path() -> Path:
    return Path(current_app.config["SERVICE_STATE_PATH"])


def _parse_service_date(value: str | None, fallback: date | None = None) -> date:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return fallback or _now().date()


def _requested_service_date() -> date:
    return _parse_service_date(request.args.get("date"), _now().date())


@blueprint.get("/")
def index() -> str:
    selected_date = _requested_service_date()
    service = build_sample_service(selected_date)
    inventory = build_inventory_summary(
        service,
        load_state(_state_path(), selected_date),
    )
    return render_template(
        "dashboard.html",
        service=service,
        inventory=inventory,
        selected_date=selected_date,
        previous_service_date=selected_date - timedelta(days=1),
        next_service_date=selected_date + timedelta(days=1),
        today=_now().date(),
        last_synced=_now(),
    )


@blueprint.post("/inventory")
def update_inventory():
    selected_date = _parse_service_date(request.form.get("service_date"))
    save_state(_state_path(), selected_date, state_from_form(request.form))
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.post("/sync")
def sync():
    """Refresh the selected service date.

    Square polling will replace this placeholder later. The date is already
    carried through so the future adapter can request the correct time range.
    """
    selected_date = _parse_service_date(request.form.get("service_date"))
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.get("/healthz")
def healthz() -> tuple[dict[str, str], int]:
    return {"status": "ok"}, 200
