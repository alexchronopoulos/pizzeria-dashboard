from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
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
    load_app_metadata,
    load_customer_history_for_order,
    load_customer_history_sync_info,
    load_customer_summaries_for_orders,
    load_order_slot_assignment_overrides,
    load_order_for_date,
    load_orders_for_date,
    load_sync_info,
    merge_orders_for_date,
    save_app_metadata,
    save_order_slot_assignment,
)
from .domain import Order, build_service_board, parse_ticket_pickup_time
from .customer_history import build_customer_visit_summary
from .customer_history_sync import sync_customer_history
from .service_config import (
    configuration_from_form,
    load_configuration,
    save_configuration,
)
from .service_state import build_inventory_summary, load_state, save_state, state_from_form
from .square_api import SquareClient, SquareError, SquareSettings
from .sync_service import configured_order_source, sync_orders_for_date, sync_sample_orders

blueprint = Blueprint("dashboard", __name__)

AUTO_REFRESH_ENABLED_KEY = "square_auto_refresh_enabled"
AUTO_REFRESH_SECONDS_KEY = "square_auto_refresh_seconds"
AUTO_REFRESH_INTERVALS = (15, 30, 60, 120, 300)


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


def _auto_refresh_preferences() -> tuple[bool, int]:
    database_path = _database_path()
    configured_seconds = int(current_app.config.get("SQUARE_AUTO_REFRESH_SECONDS", 30))
    if configured_seconds not in AUTO_REFRESH_INTERVALS:
        configured_seconds = 30
    stored_enabled = load_app_metadata(database_path, AUTO_REFRESH_ENABLED_KEY)
    stored_seconds = load_app_metadata(database_path, AUTO_REFRESH_SECONDS_KEY)
    enabled = stored_enabled != "false"
    try:
        seconds = int(stored_seconds) if stored_seconds is not None else configured_seconds
    except ValueError:
        seconds = configured_seconds
    if seconds not in AUTO_REFRESH_INTERVALS:
        seconds = configured_seconds
    return enabled, seconds


@blueprint.get("/")
def index() -> str:
    selected_date = _requested_service_date()
    now = _now()
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
    # Customer visit reporting covers every cached online order for the selected
    # date, including orders that contain only non-production items. The kitchen
    # board still filters those orders out of ``service.all_orders``.
    customer_summaries = load_customer_summaries_for_orders(
        database_path,
        (order.square_order_id or order.order_id for order in orders),
    )
    customer_visit_summary = build_customer_visit_summary(orders, customer_summaries)
    customer_history_info = load_customer_history_sync_info(database_path)
    auto_refresh_preference, auto_sync_seconds = _auto_refresh_preferences()
    square_refresh_controls_visible = (
        source == "square"
        and bool(str(current_app.config.get("SQUARE_ACCESS_TOKEN", "")).strip())
    )
    auto_sync_available = (
        square_refresh_controls_visible
        and selected_date == now.date()
    )

    return render_template(
        "dashboard.html",
        service=service,
        inventory=inventory,
        selected_date=selected_date,
        previous_service_date=selected_date - timedelta(days=1),
        next_service_date=selected_date + timedelta(days=1),
        today=now.date(),
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
        square_refresh_controls_visible=square_refresh_controls_visible,
        auto_sync_available=auto_sync_available,
        auto_sync_enabled=auto_sync_available and auto_refresh_preference,
        auto_sync_seconds=auto_sync_seconds,
        customer_summaries=customer_summaries,
        customer_visit_summary=customer_visit_summary,
        customer_history_info=customer_history_info,
    )


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
    # Older disposable cache rows may predate the explicit is_walk_in field. A
    # completed fulfillment-free order with a Ticket Name is still a walk-in and
    # should retain its pickup editor after a software update.
    is_walk_in = order.is_walk_in or bool(
        order.ticket_name
        and order.source_closed_at
        and order.fulfillment_uid is None
    )
    assigned_pickup_at, assignment_source = _effective_walk_in_assignment(
        order, selected_date, pickup_slots, assignment_overrides
    )
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    event_at = order.source_closed_at or order.source_created_at or order.pickup_at
    if event_at.tzinfo is not None:
        event_at = event_at.astimezone(timezone)
    modal_customer_name = (
        order.display_customer_name
        if is_walk_in
        else (str(order.customer_name or "").strip() or "Guest")
    )

    return render_template(
        "_order_details.html",
        order=order,
        modal_customer_name=modal_customer_name,
        is_walk_in=is_walk_in,
        assigned_pickup_at=assigned_pickup_at,
        assignment_source=assignment_source,
        pickup_slots=pickup_slots,
        selected_date=selected_date,
        today=_now().date(),
        event_at=event_at,
    )


@blueprint.get("/customer-history")
def customer_history_details():
    selected_date = _parse_service_date(request.args.get("date"))
    order_id = str(request.args.get("order_id", "")).strip()
    if not order_id:
        return render_template(
            "_customer_history.html",
            history=None,
            error="No cached order ID was supplied.",
        ), 400

    order = load_order_for_date(_database_path(), selected_date, order_id)
    if order is None:
        return render_template(
            "_customer_history.html",
            history=None,
            error="This order is no longer present in the selected date cache.",
        ), 404

    square_order_id = order.square_order_id or order.order_id
    history = load_customer_history_for_order(_database_path(), square_order_id)
    return render_template(
        "_customer_history.html",
        history=history,
        selected_order_id=square_order_id,
        service_timezone=ZoneInfo(current_app.config["SERVICE_TIMEZONE"]),
        error=None,
    )


@blueprint.post("/customer-history/rebuild")
def rebuild_customer_history():
    try:
        result = sync_customer_history(
            _database_path(),
            current_app.config,
            full=True,
            force=True,
        )
    except SquareError as exc:
        flash(f"Customer history rebuild failed: {exc}", "error")
    else:
        if result is not None:
            flash(
                f"Built customer history from {result.info.order_count} linked orders.",
                "success",
            )
            for warning in result.warnings:
                flash(warning, "warning")
    selected_date = _parse_service_date(request.form.get("service_date"))
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


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


def _updated_fulfillment_state(
    raw_order: Mapping[str, object], fulfillment_uid: str | None
) -> str | None:
    raw_fulfillments = raw_order.get("fulfillments", [])
    if not isinstance(raw_fulfillments, list):
        return None
    for fulfillment in raw_fulfillments:
        if not isinstance(fulfillment, Mapping):
            continue
        uid = str(fulfillment.get("uid", "")).strip()
        if fulfillment_uid and uid != fulfillment_uid:
            continue
        state = str(fulfillment.get("state", "")).strip().upper()
        if state:
            return state
    return None


@blueprint.post("/order-complete")
def complete_square_order():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    order = load_order_for_date(_database_path(), selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    if order.is_walk_in:
        return jsonify(ok=False, error="Walk-in orders are already completed in Square."), 400
    if order.released or order.fulfillment_state == "COMPLETED":
        return jsonify(ok=True, already_completed=True)
    if not order.square_order_id:
        return jsonify(ok=False, error="This order is not linked to Square."), 400
    if order.square_version is None:
        return jsonify(
            ok=False,
            error=(
                "Square did not provide an order version, so this order cannot be "
                "updated through the Orders API."
            ),
        ), 400
    if not order.fulfillment_uid:
        return jsonify(
            ok=False,
            error="This order does not identify a pickup fulfillment. Run a full refresh and try again.",
        ), 400

    try:
        client = SquareClient(SquareSettings.from_mapping(current_app.config))
        updated_raw = client.complete_order(
            order.square_order_id, fulfillment_uid=order.fulfillment_uid
        )
    except SquareError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    fulfillment_state = _updated_fulfillment_state(
        updated_raw, order.fulfillment_uid
    ) or order.fulfillment_state
    raw_version = updated_raw.get("version")
    try:
        square_version = int(raw_version)
    except (TypeError, ValueError):
        square_version = order.square_version

    updated_order = replace(
        order,
        released=(
            fulfillment_state == "COMPLETED"
            or str(updated_raw.get("state", "")).upper() == "COMPLETED"
        ),
        square_version=square_version,
        fulfillment_state=fulfillment_state,
        source_updated_at=_now(),
    )
    merge_orders_for_date(
        _database_path(),
        selected_date,
        (updated_order,),
        candidate_square_order_ids=(order.square_order_id,),
        source="square",
        synced_at=datetime.now(UTC),
    )

    return jsonify(
        ok=True,
        already_completed=False,
        order_state=str(updated_raw.get("state", "")).upper() or None,
        fulfillment_state=fulfillment_state,
    )


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


def _refresh_customer_history_after_order_sync(*, force: bool = False):
    """Refresh customer tags without making order sync depend on this cache."""
    try:
        return sync_customer_history(
            _database_path(),
            current_app.config,
            full=False,
            force=force,
        )
    except SquareError:
        # Customer history is a secondary, rebuildable feature. A temporary
        # Payments API failure must not stop the production order board.
        return None


@blueprint.post("/sync/quick")
def quick_sync():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        payload = request.form
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    if selected_date != _now().date():
        return jsonify(
            ok=False,
            error="Automatic refresh is available only for today's service date.",
        ), 400

    try:
        result = sync_orders_for_date(
            _database_path(),
            selected_date,
            current_app.config,
            incremental=True,
        )
    except SquareError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    history_result = _refresh_customer_history_after_order_sync()
    return jsonify(
        ok=True,
        incremental=result.incremental,
        changed_count=result.changed_count,
        removed_count=result.removed_count,
        order_count=result.info.order_count,
        candidates_scanned=result.candidates_scanned or 0,
        synced_at=result.info.synced_at.isoformat(),
        warnings=list(result.warnings),
        customer_history_changed=(
            history_result.changed_count if history_result is not None else 0
        ),
    )


@blueprint.post("/auto-refresh-settings")
def update_auto_refresh_settings():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    enabled = bool(payload.get("enabled"))
    try:
        seconds = int(payload.get("seconds", 30))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Refresh interval must be a number."), 400
    if seconds not in AUTO_REFRESH_INTERVALS:
        return jsonify(
            ok=False,
            error="Choose one of the available refresh intervals.",
        ), 400

    save_app_metadata(
        _database_path(), AUTO_REFRESH_ENABLED_KEY, "true" if enabled else "false"
    )
    save_app_metadata(_database_path(), AUTO_REFRESH_SECONDS_KEY, str(seconds))
    return jsonify(ok=True, enabled=enabled, seconds=seconds)


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
        history_result = (
            _refresh_customer_history_after_order_sync(force=True)
            if result.info.source == "square"
            else None
        )
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
        if history_result is not None:
            for warning in history_result.warnings:
                flash(warning, "warning")
    return redirect(url_for("dashboard.index", date=selected_date.isoformat()), code=303)


@blueprint.get("/healthz")
def healthz() -> tuple[dict[str, str], int]:
    return {"status": "ok"}, 200
