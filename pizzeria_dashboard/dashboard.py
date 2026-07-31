from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .database import (
    has_orders_for_date,
    load_order_slot_assignment_overrides,
    load_order_for_date,
    load_orders_for_date,
    load_sync_info,
    save_order_slot_assignment,
)
from .domain import Order, build_service_board, order_to_payload, parse_ticket_pickup_time
from .service_config import (
    configuration_from_form,
    load_configuration,
    save_configuration,
)
from .service_state import build_inventory_summary, load_state, save_state, state_from_form
from .square_api import SquareClient, SquareError, SquareSettings
from .sync_service import configured_order_source, sync_orders_for_date, sync_sample_orders

blueprint = Blueprint("dashboard", __name__)


def _now() -> datetime:
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return datetime.now(timezone)


def _database_path() -> Path:
    return Path(current_app.config["DATABASE_PATH"])


def _parse_service_date(value: str | None, fallback: date | None = None) -> date:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return fallback or _now().date()


def _requested_service_date() -> date:
    return _parse_service_date(request.args.get("date"), _now().date())


def _order_source() -> str:
    return configured_order_source(current_app.config)


def _ensure_cached_orders(service_date: date, source: str) -> None:
    database_path = _database_path()
    if (
        source == "sample"
        and current_app.config.get("AUTO_SEED_SAMPLE_DATA", True)
        and not has_orders_for_date(database_path, service_date)
    ):
        sync_sample_orders(database_path, service_date)


@blueprint.get("/")
def index() -> str:
    selected_date = _requested_service_date()
    try:
        source = _order_source()
    except SquareError as exc:
        source = "configuration-error"
        flash(str(exc), "error")
    _ensure_cached_orders(selected_date, source)

    database_path = _database_path()
    orders = load_orders_for_date(database_path, selected_date)
    service_configuration = load_configuration(database_path)
    service = build_service_board(
        selected_date,
        orders,
        pizza_capacity_per_window=int(
            current_app.config["PIZZA_CAPACITY_PER_WINDOW"]
        ),
        pickup_times=service_configuration.pickup_times(selected_date),
        walk_in_assignments=load_order_slot_assignment_overrides(
            database_path, selected_date
        ),
    )
    inventory_salad_types = tuple(
        dict.fromkeys((*service_configuration.salad_types, *service.salad_counts.keys()))
    )
    inventory_side_types = tuple(
        dict.fromkeys((*service_configuration.side_types, *service.side_counts.keys()))
    )
    inventory = build_inventory_summary(
        service,
        load_state(
            database_path,
            selected_date,
            inventory_salad_types,
            inventory_side_types,
        ),
        inventory_salad_types,
        inventory_side_types,
    )
    sync_info = load_sync_info(database_path, selected_date)

    return render_template(
        "dashboard.html",
        service=service,
        inventory=inventory,
        selected_date=selected_date,
        previous_service_date=selected_date - timedelta(days=1),
        next_service_date=selected_date + timedelta(days=1),
        today=_now().date(),
        sync_info=sync_info,
        order_source=source,
        service_configuration=service_configuration,
        selected_day_hours=service_configuration.hours_for_date(selected_date),
        square_configured=bool(
            str(current_app.config.get("SQUARE_ACCESS_TOKEN", "")).strip()
        ),
        service_timezone=ZoneInfo(current_app.config["SERVICE_TIMEZONE"]),
        last_synced=(
            sync_info.synced_at.astimezone(
                ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
            )
            if sync_info
            else None
        ),
    )


def _payment_ids(raw_order: Mapping[str, object]) -> tuple[str, ...]:
    raw_tenders = raw_order.get("tenders", [])
    if not isinstance(raw_tenders, list):
        return ()

    payment_ids: list[str] = []
    for tender in raw_tenders:
        if not isinstance(tender, Mapping):
            continue
        for key in ("payment_id", "id"):
            value = tender.get(key)
            if value:
                payment_id = str(value)
                if payment_id not in payment_ids:
                    payment_ids.append(payment_id)
                break
    return tuple(payment_ids)


def _redact_sensitive(value: object) -> object:
    """Redact credentials and stable card fingerprints from debug payloads."""
    sensitive_keys = {
        "access_token",
        "card_nonce",
        "cvv",
        "fingerprint",
        "nonce",
        "verification_token",
    }
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[redacted]"
                if str(key).casefold() in sensitive_keys
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value




def _localize_square_timestamps(value: object) -> object:
    """Convert Square RFC 3339 timestamp fields to the service timezone.

    Square commonly returns timestamps with a trailing ``Z`` (UTC). Keep the
    exact instant, but present and inspect it in the configured local timezone.
    July dates in New York are EDT (UTC-04:00); winter dates are EST (UTC-05:00).
    """
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])

    def convert(item: object, key: str | None = None) -> object:
        if isinstance(item, Mapping):
            return {str(child_key): convert(child, str(child_key)) for child_key, child in item.items()}
        if isinstance(item, list):
            return [convert(child) for child in item]
        if isinstance(item, tuple):
            return [convert(child) for child in item]
        if isinstance(item, str) and key and (key.endswith("_at") or key.endswith("_time")):
            text = item.strip()
            normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return item
            if parsed.tzinfo is None:
                return item
            return parsed.astimezone(timezone).isoformat()
        return item

    return convert(value)

def _effective_walk_in_assignment(
    order: Order,
    selected_date: date,
    pickup_slots: tuple[datetime, ...],
    overrides: Mapping[str, datetime | None],
) -> tuple[datetime | None, str | None]:
    if order.order_id in overrides:
        assigned = overrides[order.order_id]
        return assigned, "manual" if assigned is not None else "manual-unscheduled"

    parsed = parse_ticket_pickup_time(
        order.ticket_name or order.customer_name,
        selected_date,
        pickup_slots,
        reference_at=(
            order.source_closed_at or order.source_created_at or order.pickup_at
        ),
    )
    return parsed, "ticket-name" if parsed is not None else None


@blueprint.get("/order-details")
def order_details():
    selected_date = _parse_service_date(request.args.get("date"))
    order_id = str(request.args.get("order_id", "")).strip()
    if not order_id:
        return render_template("_order_details.html", order=None, error="No cached order ID was supplied."), 400

    order = load_order_for_date(_database_path(), selected_date, order_id)
    if order is None:
        return render_template("_order_details.html", order=None, error="This order is no longer present in the selected date cache."), 404

    configuration = load_configuration(_database_path())
    pickup_slots = configuration.pickup_times(selected_date)
    assignment_overrides = load_order_slot_assignment_overrides(_database_path(), selected_date)
    assigned_pickup_at, assignment_source = _effective_walk_in_assignment(
        order, selected_date, pickup_slots, assignment_overrides
    )
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    event_at = order.source_closed_at or order.source_created_at or order.pickup_at
    if event_at.tzinfo is not None:
        event_at = event_at.astimezone(timezone)

    return render_template(
        "_order_details.html",
        order=order, assigned_pickup_at=assigned_pickup_at,
        assignment_source=assignment_source, pickup_slots=pickup_slots,
        selected_date=selected_date, today=_now().date(), event_at=event_at,
    )


@blueprint.get("/order-debug")
def order_debug():
    selected_date = _parse_service_date(request.args.get("date"))
    order_id = str(request.args.get("order_id", "")).strip()
    order = load_order_for_date(_database_path(), selected_date, order_id)
    if order is None:
        return render_template("_order_debug.html", order=None, error="This order is no longer present in the selected date cache."), 404

    raw_square_order: object | None = None
    payments: tuple[object, ...] = ()
    live_error: str | None = None
    if order.square_order_id:
        try:
            client = SquareClient(SquareSettings.from_mapping(current_app.config))
            retrieved_order = client.retrieve_order(order.square_order_id)
            raw_square_order = _localize_square_timestamps(_redact_sensitive(retrieved_order))
            retrieved_payments: list[object] = []
            for payment_id in _payment_ids(retrieved_order):
                try:
                    retrieved_payments.append(_localize_square_timestamps(_redact_sensitive(client.get_payment(payment_id))))
                except SquareError as exc:
                    retrieved_payments.append({"payment_id": payment_id, "dashboard_error": str(exc)})
            payments = tuple(retrieved_payments)
        except SquareError as exc:
            live_error = str(exc)
    else:
        live_error = "This is sample data and has no live Square order document."

    return render_template(
        "_order_debug.html", order=order, cached_payload=order_to_payload(order),
        raw_square_order=raw_square_order, payments=payments, live_error=live_error,
        error=None,
    )


@blueprint.post("/inventory")
def update_inventory():
    selected_date = _parse_service_date(request.form.get("service_date"))
    configured = load_configuration(_database_path())
    submitted_salads = tuple(request.form.getlist("salad_name"))
    submitted_sides = tuple(request.form.getlist("side_name"))
    salad_types = submitted_salads or configured.salad_types
    side_types = submitted_sides or configured.side_types
    save_state(
        _database_path(),
        selected_date,
        state_from_form(request.form, salad_types, side_types),
    )
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.post("/settings")
def update_settings():
    selected_date = _parse_service_date(request.form.get("service_date"))
    configuration = configuration_from_form(request.form)
    save_configuration(_database_path(), configuration)
    flash("Service setup saved.", "success")
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.post("/walk-in-assignment")
def update_walk_in_assignment():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No order ID was supplied."), 400

    order = load_order_for_date(_database_path(), selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    if not order.is_walk_in:
        return jsonify(ok=False, error="Only walk-in orders can be reassigned."), 400

    raw_pickup_at = str(payload.get("pickup_at", "")).strip()
    pickup_at: datetime | None = None
    if raw_pickup_at:
        try:
            pickup_at = datetime.fromisoformat(raw_pickup_at)
        except ValueError:
            return jsonify(ok=False, error="The pickup time is invalid."), 400
        if pickup_at.date() != selected_date:
            return jsonify(ok=False, error="The pickup time is on another day."), 400

        configuration = load_configuration(_database_path())
        allowed_slots = {
            value.replace(tzinfo=None)
            for value in configuration.pickup_times(selected_date)
        }
        if pickup_at.replace(tzinfo=None) not in allowed_slots:
            return jsonify(ok=False, error="Choose one of the configured service slots."), 400

    save_order_slot_assignment(
        _database_path(), selected_date, order_id, pickup_at
    )
    return jsonify(ok=True)


@blueprint.post("/sync")
def sync():
    selected_date = _parse_service_date(request.form.get("service_date"))
    try:
        result = sync_orders_for_date(
            _database_path(), selected_date, current_app.config
        )
    except SquareError as exc:
        flash(f"Square sync failed: {exc}", "error")
    else:
        if result.info.source == "square":
            scanned = (
                f" from {result.candidates_scanned} Square order candidates"
                if result.candidates_scanned is not None
                else ""
            )
            location = f" at {result.location_name}" if result.location_name else ""
            flash(
                f"Pulled {result.info.order_count} orders for "
                f"{selected_date.strftime('%B %-d')}{location}{scanned}.",
                "success",
            )
        else:
            flash(
                f"Loaded {result.info.order_count} sample orders for "
                f"{selected_date.strftime('%B %-d')}.",
                "success",
            )
        for warning in result.warnings:
            flash(warning, "warning")
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.get("/healthz")
def healthz() -> tuple[dict[str, str], int]:
    return {"status": "ok"}, 200
