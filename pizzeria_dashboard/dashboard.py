from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo
from uuid import uuid4

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
    delete_order_slot_assignment,
    has_orders_for_date,
    load_app_metadata,
    load_board_content_revision,
    load_customer_history_for_order,
    load_customer_history_sync_info,
    load_customer_summaries_for_orders,
    load_vip_customer_keys,
    load_latest_service_state_before,
    load_order_slot_assignment_overrides,
    load_order_for_date,
    load_order_internal_note,
    load_order_internal_notes_for_date,
    load_order_ready_states,
    load_orders_for_date,
    load_pie_production_states,
    load_prep_assignees,
    load_prep_recipes,
    load_prep_tasks_for_date,
    load_service_state_payload,
    load_service_notes_for_date,
    load_sync_info,
    merge_orders_for_date,
    prune_pie_production_states,
    reorder_prep_tasks,
    remove_order_from_dashboard,
    save_app_metadata,
    save_manual_order,
    save_order_internal_note,
    save_order_ready_state,
    save_order_slot_assignment,
    save_prep_assignee,
    save_prep_recipe,
    save_prep_task,
    save_service_note,
    save_vip_customer,
    delete_prep_assignee,
    delete_prep_recipe,
    delete_prep_task,
    delete_vip_customers,
    touch_board_content_revision,
    update_prep_recipe,
    update_prep_task,
    update_pie_production_state,
)
from .domain import (
    Item,
    Order,
    PickupWindow,
    ServiceBoard,
    build_service_board,
    parse_ticket_pickup_time,
)
from .customer_history import build_customer_visit_summary
from .customer_history_sync import sync_customer_history
from .service_config import (
    configuration_from_form,
    load_configuration,
    save_configuration,
)
from .service_state import (
    build_inventory_summary,
    carryover_state,
    load_state,
    save_state,
    state_from_form,
    state_from_payload,
)
from .square_api import SquareAPIError, SquareClient, SquareError, SquareSettings
from .sync_service import configured_order_source, sync_orders_for_date, sync_sample_orders

blueprint = Blueprint("dashboard", __name__)

AUTO_REFRESH_ENABLED_KEY = "square_auto_refresh_enabled"
AUTO_REFRESH_SECONDS_KEY = "square_auto_refresh_seconds"
AUTO_REFRESH_INTERVALS = (10, 15, 30, 60, 120, 300)
MANUAL_ITEM_CATEGORIES = {
    "pizza",
    "salad",
    "side",
    "dessert",
    "merch",
    "drink",
    "other",
}


def _now() -> datetime:
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return datetime.now(timezone)


def _local_service_time(value: datetime) -> datetime:
    """Normalize a timestamp to timezone-neutral local service wall time."""
    if value.tzinfo is None:
        return value
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return value.astimezone(timezone).replace(tzinfo=None)


def _vip_name_key(order: Order) -> str | None:
    if order.is_walk_in:
        return None
    name = " ".join(str(order.customer_name or "").split()).casefold()
    if not name or name in {"guest", "walk-in", "walk in"}:
        return None
    return f"name:{name}"


def _vip_keys_for_order(order: Order, customer_summary=None) -> tuple[str, ...]:
    keys: list[str] = []
    if customer_summary is not None and getattr(customer_summary, "customer_id", None):
        keys.append(f"square:{customer_summary.customer_id}")
    name_key = _vip_name_key(order)
    if name_key:
        keys.append(name_key)
    return tuple(dict.fromkeys(keys))


def _vip_order_ids(
    orders: tuple[Order, ...], customer_summaries: Mapping[str, object], vip_keys: set[str]
) -> set[str]:
    result: set[str] = set()
    for order in orders:
        summary = customer_summaries.get(order.square_order_id or order.order_id)
        if any(key in vip_keys for key in _vip_keys_for_order(order, summary)):
            result.add(order.order_id)
    return result


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


def _raw_square_order_is_unpaid_open(raw_order: Mapping[str, object]) -> bool:
    """Return whether Square currently shows an OPEN order with no tenders."""
    if str(raw_order.get("state", "")).upper() != "OPEN":
        return False
    raw_tenders = raw_order.get("tenders", [])
    if not isinstance(raw_tenders, list):
        return True
    return not any(isinstance(value, Mapping) for value in raw_tenders)


def _raw_square_order_payment_ids(raw_order: Mapping[str, object]) -> tuple[str, ...]:
    """Return unique Payment IDs attached to the Square order's tenders."""
    raw_tenders = raw_order.get("tenders", [])
    if not isinstance(raw_tenders, list):
        return ()
    return tuple(
        dict.fromkeys(
            payment_id
            for tender in raw_tenders
            if isinstance(tender, Mapping)
            and (payment_id := str(tender.get("payment_id") or "").strip())
        )
    )


def _raw_square_order_reference_id(raw_order: Mapping[str, object]) -> str | None:
    value = str(raw_order.get("reference_id") or "").strip()
    return value or None


def _ensure_cached_orders(service_date: date, source: str) -> None:
    database_path = _database_path()
    if (
        source == "sample"
        and current_app.config.get("AUTO_SEED_SAMPLE_DATA", True)
        and not has_orders_for_date(database_path, service_date)
    ):
        sync_sample_orders(database_path, service_date)


def _production_pie_keys(service_date: date, orders: tuple[Order, ...]) -> tuple[str, ...]:
    """Return one persistent production-state key for every physical pizza.

    Keep the historical line-item key for the first unit so existing single-pie
    timer state survives upgrades; additional quantity units receive a suffix.
    """
    keys: list[str] = []
    for order in orders:
        order_key = order.square_order_id or order.order_id
        for index, item in enumerate(order.production_items):
            if item.category != "pizza":
                continue
            item_key = item.catalog_object_id or item.name
            base_key = f"{service_date.isoformat()}|{order_key}|{item_key}|{index}"
            for unit_index in range(item.quantity):
                keys.append(base_key if unit_index == 0 else f"{base_key}|{unit_index}")
    return tuple(keys)


def _order_prep_buffer_minutes() -> int:
    return max(int(current_app.config.get("ORDER_PREP_BUFFER_MINUTES", 20)), 0)


def _pickup_windows_after_prep_buffer(
    service: ServiceBoard, selected_date: date, now: datetime
) -> tuple[PickupWindow, ...]:
    """Return service windows that are still far enough away to accept an order."""
    windows = tuple(service.windows)
    if selected_date != now.date():
        return windows

    cutoff = now.replace(tzinfo=None) + timedelta(
        minutes=_order_prep_buffer_minutes()
    )
    return tuple(window for window in windows if window.pickup_at >= cutoff)


def _available_pickup_windows(
    service: ServiceBoard, selected_date: date, now: datetime
) -> tuple[PickupWindow, ...]:
    """Return pickup windows still usable for walk-in pizza capacity.

    The production team needs a full preparation window before pickup. Historical
    dates retain their old availability display for review; future dates have no
    elapsed slots, while today's list closes each slot before the prep cutoff.
    """
    return tuple(
        window
        for window in _pickup_windows_after_prep_buffer(service, selected_date, now)
        if service.open_capacity(window) > 0
    )


def _online_order_slot_reserve(
    windows: tuple[PickupWindow, ...],
    *,
    pizzas_per_online_order_slot: int,
) -> tuple[int, int]:
    """Return (online pizza capacity still sellable, dough reserved for it).

    Each 15-minute pickup slot can expose a configurable number of *pizzas* to
    online ordering. One pizza equals one dough ball. Existing unreleased online
    pizza quantities consume that capacity; walk-ins and dashboard-only manual
    orders do not. Releasing a Square order's capacity removes its pizza quantity
    from the consumed-online total, immediately reserving those dough balls again.
    """
    max_pizzas = max(int(pizzas_per_online_order_slot), 0)
    available_online_pizzas = 0
    for window in windows:
        active_online_pizzas = sum(
            order.pizza_units
            for order in window.orders
            if order.pizza_units > 0
            and not order.is_walk_in
            and not order.is_manual
            and not order.released
            and order.fulfillment_state != "COMPLETED"
        )
        available_online_pizzas += max(max_pizzas - active_online_pizzas, 0)

    # One online pizza always reserves one dough ball.
    return available_online_pizzas, available_online_pizzas


def _auto_refresh_preferences() -> tuple[bool, int]:
    database_path = _database_path()
    configured_seconds = int(current_app.config.get("SQUARE_AUTO_REFRESH_SECONDS", 10))
    if configured_seconds not in AUTO_REFRESH_INTERVALS:
        configured_seconds = 10
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
    prune_pie_production_states(
        database_path, selected_date, _production_pie_keys(selected_date, orders)
    )
    internal_notes = load_order_internal_notes_for_date(database_path, selected_date)
    service_notes = load_service_notes_for_date(database_path, selected_date)
    prep_tasks = load_prep_tasks_for_date(database_path, selected_date)
    prep_recipe_count = len(load_prep_recipes(database_path))
    pie_states = load_pie_production_states(database_path, selected_date)
    ready_states = load_order_ready_states(database_path, selected_date)
    board_content_revision = load_board_content_revision(database_path, selected_date)
    service_configuration = load_configuration(database_path)
    pickup_overrides = load_order_slot_assignment_overrides(
        database_path, selected_date
    )
    service = build_service_board(
        selected_date,
        orders,
        pizza_capacity_per_window=int(
            current_app.config["PIZZA_CAPACITY_PER_WINDOW"]
        ),
        pickup_times=service_configuration.pickup_times(selected_date),
        pickup_time_overrides=pickup_overrides,
    )
    inventory_salad_types = tuple(
        dict.fromkeys((*service_configuration.salad_types, *service.salad_counts.keys()))
    )
    inventory_side_types = tuple(
        dict.fromkeys((*service_configuration.side_types, *service.side_counts.keys()))
    )
    saved_state_payload = load_service_state_payload(database_path, selected_date)
    if saved_state_payload is not None:
        inventory_state = state_from_payload(
            saved_state_payload, inventory_salad_types, inventory_side_types
        )
    else:
        previous_saved_state = load_latest_service_state_before(
            database_path, selected_date
        )
        if previous_saved_state is None:
            inventory_state = load_state(
                database_path,
                selected_date,
                inventory_salad_types,
                inventory_side_types,
            )
        else:
            previous_date, previous_payload = previous_saved_state
            previous_state = state_from_payload(
                previous_payload,
                service_configuration.salad_types,
                service_configuration.side_types,
            )
            inventory_state = carryover_state(
                previous_state,
                load_orders_for_date(database_path, previous_date),
                service_configuration.salad_types,
                service_configuration.side_types,
            )
        # Freeze the inherited opening inventory once the service date actually
        # arrives. This gives the next service day a reliable carryover source
        # even when today's inherited counts need no manual adjustment.
        if selected_date == now.date():
            save_state(database_path, selected_date, inventory_state)
    current_service_time = now.replace(tzinfo=None)
    buffer_eligible_windows = _pickup_windows_after_prep_buffer(
        service, selected_date, now
    )
    available_pickup_windows = tuple(
        window
        for window in buffer_eligible_windows
        if service.open_capacity(window) > 0
    )
    future_one_pie_windows = tuple(
        window
        for window in available_pickup_windows
        if service.open_capacity(window) == 1
    )
    future_two_pie_windows = tuple(
        window
        for window in available_pickup_windows
        if service.open_capacity(window) >= 2
    )
    if selected_date >= now.date():
        online_order_available_pizzas, online_order_dough_reserve = _online_order_slot_reserve(
            buffer_eligible_windows,
            pizzas_per_online_order_slot=(
                service_configuration.pizzas_per_online_order_slot
            ),
        )
    else:
        online_order_available_pizzas = 0
        online_order_dough_reserve = 0
    inventory = build_inventory_summary(
        service,
        inventory_state,
        inventory_salad_types,
        inventory_side_types,
        orders=orders,
        open_slot_dough_reserve=online_order_dough_reserve,
    )
    sync_info = load_sync_info(database_path, selected_date)
    # Customer visit reporting covers every cached online order for the selected
    # date, including orders that contain only non-production items. The kitchen
    # board still filters those orders out of ``service.all_orders``.
    customer_summaries = load_customer_summaries_for_orders(
        database_path,
        (order.square_order_id or order.order_id for order in orders),
    )
    vip_customer_keys = load_vip_customer_keys(database_path)
    vip_order_ids = _vip_order_ids(orders, customer_summaries, vip_customer_keys)
    customer_visit_summary = build_customer_visit_summary(orders, customer_summaries)
    customer_history_info = load_customer_history_sync_info(database_path)
    auto_refresh_preference, auto_sync_seconds = _auto_refresh_preferences()
    square_refresh_controls_visible = (
        source == "square"
        and bool(str(current_app.config.get("SQUARE_ACCESS_TOKEN", "")).strip())
    )
    incremental_sync_available = (
        square_refresh_controls_visible
        and selected_date >= now.date()
    )
    auto_sync_available = (
        square_refresh_controls_visible
        and selected_date >= now.date()
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
        current_service_time=current_service_time,
        order_prep_buffer_minutes=_order_prep_buffer_minutes(),
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
        incremental_sync_available=incremental_sync_available,
        auto_sync_available=auto_sync_available,
        auto_sync_enabled=auto_sync_available and auto_refresh_preference,
        auto_sync_seconds=auto_sync_seconds,
        customer_summaries=customer_summaries,
        vip_order_ids=vip_order_ids,
        customer_visit_summary=customer_visit_summary,
        customer_history_info=customer_history_info,
        internal_notes=internal_notes,
        service_notes=service_notes,
        service_notes_latest_id=(service_notes[-1].note_id if service_notes else 0),
        prep_open_task_count=sum(1 for prep_task in prep_tasks if not prep_task.completed),
        prep_recipe_count=prep_recipe_count,
        pie_states=pie_states,
        boxed_orders={
            order_id: boxed_at.astimezone(
                ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
            )
            for order_id, boxed_at in ready_states.items()
        },
        board_content_revision=board_content_revision,
        future_one_pie_windows=future_one_pie_windows,
        future_two_pie_windows=future_two_pie_windows,
        online_order_available_pizzas=online_order_available_pizzas,
        pizzas_per_online_order_slot=service_configuration.pizzas_per_online_order_slot,
        pickup_overrides=pickup_overrides,
        original_pickup_times={
            order.order_id: _local_service_time(order.pickup_at)
            for order in orders
        },
        manual_order_default_date=(
            selected_date if selected_date >= now.date() else now.date()
        ),
    )


@blueprint.post("/manual-order")
def create_manual_order():
    """Create a dashboard-only verbal order without touching Square."""
    customer_name = str(request.form.get("customer_name", "")).strip()
    if not customer_name:
        flash("Enter a customer name for the manual order.", "error")
        return redirect(url_for("dashboard.index", date=_requested_service_date().isoformat()), code=303)
    if len(customer_name) > 120:
        flash("Customer name must be 120 characters or fewer.", "error")
        return redirect(url_for("dashboard.index", date=_requested_service_date().isoformat()), code=303)

    raw_date = str(request.form.get("pickup_date", "")).strip()
    raw_time = str(request.form.get("pickup_time", "")).strip()
    try:
        pickup_date = date.fromisoformat(raw_date)
        pickup_at = datetime.fromisoformat(f"{raw_date}T{raw_time}")
    except ValueError:
        flash("Enter a valid pickup date and time.", "error")
        return redirect(url_for("dashboard.index", date=_requested_service_date().isoformat()), code=303)

    now = _now()
    current_service_time = now.replace(tzinfo=None)
    if pickup_date < now.date() or pickup_at < current_service_time:
        flash("Manual orders must use a pickup time that has not passed.", "error")
        return redirect(url_for("dashboard.index", date=max(pickup_date, now.date()).isoformat()), code=303)

    item_names = request.form.getlist("item_name")
    item_quantities = request.form.getlist("item_quantity")
    item_categories = request.form.getlist("item_category")
    if not (len(item_names) == len(item_quantities) == len(item_categories)):
        flash("The manual order item rows were incomplete. Please try again.", "error")
        return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)

    items: list[Item] = []
    for raw_name, raw_quantity, raw_category in zip(
        item_names, item_quantities, item_categories
    ):
        name = str(raw_name).strip()
        if not name:
            continue
        if len(name) > 160:
            flash("Manual item names must be 160 characters or fewer.", "error")
            return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)
        category = str(raw_category).strip().lower()
        if category not in MANUAL_ITEM_CATEGORIES:
            flash(f"Choose a valid item type for {name}.", "error")
            return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1 or quantity > 50:
            flash(f"Quantity for {name} must be between 1 and 50.", "error")
            return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)
        items.append(Item(name=name, quantity=quantity, category=category))

    if not items:
        flash("Add at least one item to the manual order.", "error")
        return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)

    order = Order(
        order_id=f"manual-{uuid4().hex}",
        customer_name=customer_name,
        pickup_at=pickup_at,
        items=tuple(items),
        source_created_at=now,
        creation_product="MANUAL_DASHBOARD",
    )
    save_manual_order(_database_path(), pickup_date, order)
    flash(
        f"Added manual order for {order.display_customer_name} at {pickup_at.strftime('%-I:%M %p')}.",
        "success",
    )
    return redirect(url_for("dashboard.index", date=pickup_date.isoformat()), code=303)


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

    # Direct modal requests can arrive before the main board has been opened.
    # First check the existing local/cache data so opening an already-present
    # manual order never seeds unrelated sample orders for that date. Only if
    # the requested order is absent do we initialize disposable sample data,
    # matching index() behavior. Square-backed dates are never fetched here;
    # they continue to use whatever has already been cached by an explicit sync.
    try:
        source = _order_source()
    except SquareError:
        source = "configuration-error"

    database_path = _database_path()
    order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        _ensure_cached_orders(selected_date, source)
        order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        return render_template("_order_details.html", order=None, error="This order is no longer present in the selected date cache."), 404

    configuration = load_configuration(_database_path())
    pickup_slots = configuration.pickup_times(selected_date)
    current_service_time = _now().replace(tzinfo=None)
    selectable_pickup_slots = (
        tuple(slot for slot in pickup_slots if slot >= current_service_time)
        if selected_date == current_service_time.date()
        else pickup_slots
    )
    assignment_overrides = load_order_slot_assignment_overrides(_database_path(), selected_date)
    # Older disposable cache rows may predate the explicit is_walk_in field. A
    # completed fulfillment-free order with a Ticket Name is still a walk-in and
    # should retain its pickup editor after a software update.
    is_walk_in = order.is_walk_in or bool(
        order.ticket_name
        and order.source_closed_at
        and order.fulfillment_uid is None
    )
    is_manual = order.is_manual
    assigned_pickup_at, assignment_source = _effective_walk_in_assignment(
        order, selected_date, pickup_slots, assignment_overrides
    )
    original_pickup_at = _local_service_time(order.pickup_at)
    scheduled_pickup_override = (
        assignment_overrides.get(order.order_id)
        if not is_walk_in and order.order_id in assignment_overrides
        else None
    )
    effective_pickup_at = (
        scheduled_pickup_override or original_pickup_at
        if not is_walk_in
        else assigned_pickup_at
    )
    service = build_service_board(
        selected_date,
        load_orders_for_date(database_path, selected_date),
        pizza_capacity_per_window=int(
            current_app.config["PIZZA_CAPACITY_PER_WINDOW"]
        ),
        pickup_times=pickup_slots,
        pickup_time_overrides=assignment_overrides,
    )
    pickup_slot_loads = {
        window.pickup_at: window.pizza_units for window in service.windows
    }
    order_window = next(
        (
            window
            for window in service.windows
            if any(candidate.order_id == order.order_id for candidate in window.orders)
        ),
        None,
    )
    release_candidate = bool(
        order_window
        and not is_manual
        and service.is_release_candidate(order, order_window)
    )
    capacity_released = bool(
        order.released or order.fulfillment_state == "COMPLETED"
    )
    can_release_capacity = bool(
        release_candidate
        and order.square_order_id
        and order.square_version is not None
        and order.fulfillment_uid
        and not is_walk_in
        and not capacity_released
        and order.is_paid is not False
    )
    capacity_release_pickup_at = order_window.pickup_at if order_window else None
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    event_at = order.source_closed_at or order.source_created_at or order.pickup_at
    if event_at.tzinfo is not None:
        event_at = event_at.astimezone(timezone)
    modal_customer_name = (
        order.display_customer_name
        if is_walk_in
        else (str(order.customer_name or "").strip() or "Guest")
    )
    can_remove_unpaid_order = bool(
        not is_walk_in
        and order.square_order_id
        and order.square_order_state == "OPEN"
        and order.is_paid is False
    )
    debug_order_id = order.square_order_id or order.order_id
    debug_reference_id = order.reference_id
    debug_payment_ids = order.payment_ids

    # Order details are also the debugging view. Older cache documents predate
    # reference/payment identifiers, so retrieve the current Square document when
    # those values are missing. Reuse the same lookup for unpaid-order eligibility
    # rather than making a second API request.
    live_order: Mapping[str, object] | None = None
    needs_live_square_order = bool(
        order.square_order_id
        and source == "square"
        and (
            order.reference_id is None
            or not order.payment_ids
            or (
                not is_walk_in
                and (order.square_order_state is None or order.is_paid is None)
            )
        )
    )
    if needs_live_square_order:
        square_client = SquareClient(
            SquareSettings.from_mapping(current_app.config)
        )
        retrieve_order = getattr(square_client, "retrieve_order", None)
        if callable(retrieve_order):
            try:
                live_order = retrieve_order(order.square_order_id)
            except SquareError:
                pass

    if live_order is not None:
        debug_reference_id = (
            _raw_square_order_reference_id(live_order) or debug_reference_id
        )
        live_payment_ids = _raw_square_order_payment_ids(live_order)
        if live_payment_ids:
            debug_payment_ids = live_payment_ids
        if (
            not is_walk_in
            and (order.square_order_state is None or order.is_paid is None)
        ):
            can_remove_unpaid_order = _raw_square_order_is_unpaid_open(live_order)

    order_summary = load_customer_summaries_for_orders(
        database_path, (order.square_order_id or order.order_id,)
    ).get(order.square_order_id or order.order_id)
    vip_keys = _vip_keys_for_order(order, order_summary)
    saved_vip_keys = load_vip_customer_keys(database_path)
    is_vip = any(key in saved_vip_keys for key in vip_keys)

    return render_template(
        "_order_details.html",
        order=order,
        modal_customer_name=modal_customer_name,
        is_walk_in=is_walk_in,
        is_manual=is_manual,
        assigned_pickup_at=assigned_pickup_at,
        assignment_source=assignment_source,
        original_pickup_at=original_pickup_at,
        scheduled_pickup_override=scheduled_pickup_override,
        effective_pickup_at=effective_pickup_at,
        pickup_slot_loads=pickup_slot_loads,
        pizza_capacity_per_window=service.pizza_capacity_per_window,
        pickup_slots=selectable_pickup_slots,
        current_service_time=current_service_time,
        selected_date=selected_date,
        today=_now().date(),
        event_at=event_at,
        internal_note=load_order_internal_note(
            database_path, selected_date, order_id
        ),
        can_remove_unpaid_order=can_remove_unpaid_order,
        release_candidate=release_candidate,
        capacity_released=capacity_released,
        can_release_capacity=can_release_capacity,
        capacity_release_pickup_at=capacity_release_pickup_at,
        debug_order_id=debug_order_id,
        debug_reference_id=debug_reference_id,
        debug_payment_ids=debug_payment_ids,
        is_vip=is_vip,
        can_toggle_vip=bool(vip_keys),
    )


@blueprint.post("/order-vip")
def update_order_vip():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    database_path = _database_path()
    order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    if order.is_walk_in:
        return jsonify(ok=False, error="Walk-in orders cannot be linked reliably to a VIP customer."), 400

    summary = load_customer_summaries_for_orders(
        database_path, (order.square_order_id or order.order_id,)
    ).get(order.square_order_id or order.order_id)
    keys = _vip_keys_for_order(order, summary)
    if not keys:
        return jsonify(ok=False, error="This order does not have a usable customer identity."), 400

    vip = bool(payload.get("vip"))
    if vip:
        preferred_key = keys[0]
        save_vip_customer(database_path, preferred_key, order.display_customer_name, vip=True)
    else:
        delete_vip_customers(database_path, keys)
    revision = touch_board_content_revision(database_path, selected_date)
    return jsonify(ok=True, vip=vip, board_content_revision=revision)


def _prep_task_json(task) -> dict[str, object]:
    return {
        "id": task.task_id,
        "task": task.task,
        "assignee": task.assignee or "",
        "completed": bool(task.completed),
        "sort_order": task.sort_order,
        "updated_at": task.updated_at.isoformat(),
    }


def _prep_recipe_json(recipe) -> dict[str, object]:
    return {
        "id": recipe.recipe_id,
        "name": recipe.name,
        "body": recipe.body,
        "updated_at": recipe.updated_at.isoformat(),
    }


@blueprint.get("/prep-list")
def prep_list_data():
    selected_date = _parse_service_date(request.args.get("date"))
    database_path = _database_path()
    tasks = load_prep_tasks_for_date(database_path, selected_date)
    return jsonify(
        ok=True,
        service_date=selected_date.isoformat(),
        tasks=[_prep_task_json(task) for task in tasks],
        assignees=list(load_prep_assignees(database_path)),
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.post("/prep-list/reorder")
def reorder_prep_list():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_task_ids = payload.get("task_ids")
    if not isinstance(raw_task_ids, list) or not all(
        isinstance(task_id, int) and not isinstance(task_id, bool) for task_id in raw_task_ids
    ):
        return jsonify(ok=False, error="Prep task order must be a list of task IDs."), 400
    database_path = _database_path()
    try:
        tasks = reorder_prep_tasks(database_path, selected_date, raw_task_ids)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(
        ok=True,
        tasks=[_prep_task_json(task) for task in tasks],
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.post("/prep-task")
def add_prep_task():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_task = payload.get("task", "")
    if not isinstance(raw_task, str):
        return jsonify(ok=False, error="The prep task must be text."), 400
    task_text = " ".join(raw_task.replace("\r", " ").replace("\n", " ").split()).strip()
    if not task_text:
        return jsonify(ok=False, error="Enter a prep task before adding it."), 400
    if len(task_text) > 300:
        return jsonify(ok=False, error="Prep tasks are limited to 300 characters."), 400
    raw_assignee = payload.get("assignee", "")
    if raw_assignee is not None and not isinstance(raw_assignee, str):
        return jsonify(ok=False, error="The assignee must be text."), 400
    assignee = " ".join(str(raw_assignee or "").split()).strip()
    if len(assignee) > 80:
        return jsonify(ok=False, error="Assignee names are limited to 80 characters."), 400
    database_path = _database_path()
    saved = save_prep_task(database_path, selected_date, task_text, assignee=assignee or None)
    return jsonify(
        ok=True,
        task=_prep_task_json(saved),
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.post("/prep-task/<int:task_id>")
def edit_prep_task(task_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    kwargs: dict[str, object] = {}
    if "task" in payload:
        raw_task = payload.get("task")
        if not isinstance(raw_task, str):
            return jsonify(ok=False, error="The prep task must be text."), 400
        task_text = " ".join(raw_task.replace("\r", " ").replace("\n", " ").split()).strip()
        if not task_text:
            return jsonify(ok=False, error="Prep tasks cannot be blank."), 400
        if len(task_text) > 300:
            return jsonify(ok=False, error="Prep tasks are limited to 300 characters."), 400
        kwargs["task"] = task_text
    if "assignee" in payload:
        raw_assignee = payload.get("assignee")
        if raw_assignee is not None and not isinstance(raw_assignee, str):
            return jsonify(ok=False, error="The assignee must be text."), 400
        assignee = " ".join(str(raw_assignee or "").split()).strip()
        if len(assignee) > 80:
            return jsonify(ok=False, error="Assignee names are limited to 80 characters."), 400
        kwargs["assignee"] = assignee or None
    if "completed" in payload:
        if not isinstance(payload.get("completed"), bool):
            return jsonify(ok=False, error="The completed value must be true or false."), 400
        kwargs["completed"] = bool(payload.get("completed"))
    if not kwargs:
        return jsonify(ok=False, error="No prep task changes were supplied."), 400
    database_path = _database_path()
    try:
        saved = update_prep_task(database_path, selected_date, task_id, **kwargs)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if saved is None:
        return jsonify(ok=False, error="That prep task no longer exists."), 404
    return jsonify(
        ok=True,
        task=_prep_task_json(saved),
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.delete("/prep-task/<int:task_id>")
def remove_prep_task(task_id: int):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    database_path = _database_path()
    if not delete_prep_task(database_path, selected_date, task_id):
        return jsonify(ok=False, error="That prep task no longer exists."), 404
    return jsonify(
        ok=True,
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.post("/prep-assignee")
def add_prep_assignee():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_name = payload.get("name", "")
    if not isinstance(raw_name, str):
        return jsonify(ok=False, error="The team member name must be text."), 400
    name = " ".join(raw_name.split()).strip()
    if not name:
        return jsonify(ok=False, error="Enter a team member name."), 400
    if len(name) > 80:
        return jsonify(ok=False, error="Team member names are limited to 80 characters."), 400
    database_path = _database_path()
    saved_name = save_prep_assignee(database_path, name, service_date=selected_date)
    return jsonify(
        ok=True,
        name=saved_name,
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.delete("/prep-assignee")
def remove_prep_assignee():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_name = payload.get("name", "")
    if not isinstance(raw_name, str):
        return jsonify(ok=False, error="The team member name must be text."), 400
    database_path = _database_path()
    if not delete_prep_assignee(database_path, raw_name, service_date=selected_date):
        return jsonify(ok=False, error="That team member is no longer in the picker."), 404
    return jsonify(
        ok=True,
        board_content_revision=load_board_content_revision(database_path, selected_date),
    )


@blueprint.get("/recipes")
def recipe_library_data():
    recipes = load_prep_recipes(_database_path())
    return jsonify(ok=True, recipes=[_prep_recipe_json(recipe) for recipe in recipes])


@blueprint.post("/recipe")
def add_prep_recipe():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_name = payload.get("name", "")
    raw_body = payload.get("body", "")
    if not isinstance(raw_name, str) or not isinstance(raw_body, str):
        return jsonify(ok=False, error="Recipe name and text must be text."), 400
    name = " ".join(raw_name.split()).strip()
    body = raw_body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not name or not body:
        return jsonify(ok=False, error="Enter both a recipe name and recipe text."), 400
    if len(name) > 120:
        return jsonify(ok=False, error="Recipe names are limited to 120 characters."), 400
    if len(body) > 20_000:
        return jsonify(ok=False, error="Recipes are limited to 20,000 characters."), 400
    database_path = _database_path()
    saved = save_prep_recipe(database_path, name, body)
    revision = touch_board_content_revision(database_path, selected_date)
    return jsonify(ok=True, recipe=_prep_recipe_json(saved), board_content_revision=revision)


@blueprint.post("/recipe/<int:recipe_id>")
def edit_prep_recipe(recipe_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_name = payload.get("name", "")
    raw_body = payload.get("body", "")
    if not isinstance(raw_name, str) or not isinstance(raw_body, str):
        return jsonify(ok=False, error="Recipe name and text must be text."), 400
    name = " ".join(raw_name.split()).strip()
    body = raw_body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not name or not body:
        return jsonify(ok=False, error="Enter both a recipe name and recipe text."), 400
    if len(name) > 120:
        return jsonify(ok=False, error="Recipe names are limited to 120 characters."), 400
    if len(body) > 20_000:
        return jsonify(ok=False, error="Recipes are limited to 20,000 characters."), 400
    database_path = _database_path()
    try:
        saved = update_prep_recipe(database_path, recipe_id, name, body)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if saved is None:
        return jsonify(ok=False, error="That recipe no longer exists."), 404
    revision = touch_board_content_revision(database_path, selected_date)
    return jsonify(ok=True, recipe=_prep_recipe_json(saved), board_content_revision=revision)


@blueprint.delete("/recipe/<int:recipe_id>")
def remove_prep_recipe(recipe_id: int):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    database_path = _database_path()
    if not delete_prep_recipe(database_path, recipe_id):
        return jsonify(ok=False, error="That recipe no longer exists."), 404
    revision = touch_board_content_revision(database_path, selected_date)
    return jsonify(ok=True, board_content_revision=revision)


@blueprint.post("/service-note")
def add_service_note():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    raw_note = payload.get("note", "")
    if raw_note is None:
        raw_note = ""
    if not isinstance(raw_note, str):
        return jsonify(ok=False, error="The service note must be text."), 400
    normalized = raw_note.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return jsonify(ok=False, error="Enter a service note before adding it."), 400
    if len(normalized) > 2000:
        return jsonify(ok=False, error="Service notes are limited to 2,000 characters."), 400

    database_path = _database_path()
    saved = save_service_note(database_path, selected_date, normalized)
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    local_created_at = saved.created_at.astimezone(timezone)
    return jsonify(
        ok=True,
        note={
            "id": saved.note_id,
            "text": saved.note,
            "created_at": local_created_at.isoformat(),
            "created_label": local_created_at.strftime("%a %-I:%M %p"),
        },
        board_content_revision=load_board_content_revision(
            database_path, selected_date
        ),
    )


@blueprint.post("/order-note")
def update_order_internal_note():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    # Validate the submitted value before looking up the order so malformed or
    # oversized notes consistently return 400 rather than being masked by a
    # missing/stale cache row.
    raw_note = payload.get("note", "")
    if raw_note is None:
        raw_note = ""
    if not isinstance(raw_note, str):
        return jsonify(ok=False, error="The note must be text."), 400
    normalized_note = raw_note.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized_note) > 2000:
        return jsonify(ok=False, error="Staff notes are limited to 2,000 characters."), 400

    try:
        source = _order_source()
    except SquareError:
        source = "configuration-error"
    _ensure_cached_orders(selected_date, source)

    database_path = _database_path()
    if load_order_for_date(database_path, selected_date, order_id) is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404

    saved_note = save_order_internal_note(
        database_path, selected_date, order_id, normalized_note
    )
    return jsonify(
        ok=True,
        note=saved_note,
        board_content_revision=load_board_content_revision(
            database_path, selected_date
        ),
    )


@blueprint.post("/scheduled-pickup-time")
def update_scheduled_pickup_time():
    """Save or clear a dashboard-only pickup-time override."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    try:
        source = _order_source()
    except SquareError:
        source = "configuration-error"
    _ensure_cached_orders(selected_date, source)

    database_path = _database_path()
    order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    is_walk_in = order.is_walk_in or bool(
        order.ticket_name
        and order.source_closed_at
        and order.fulfillment_uid is None
    )
    if is_walk_in:
        return jsonify(
            ok=False,
            error="Use the walk-in pickup editor for this order.",
        ), 400

    raw_pickup_at = str(payload.get("pickup_at", "")).strip()
    if raw_pickup_at == "original":
        delete_order_slot_assignment(database_path, selected_date, order_id)
        return jsonify(
            ok=True,
            overridden=False,
            pickup_at=_local_service_time(order.pickup_at).isoformat(),
            board_content_revision=load_board_content_revision(
                database_path, selected_date
            ),
        )

    if not raw_pickup_at:
        return jsonify(ok=False, error="Choose a pickup time."), 400
    try:
        pickup_at = _local_service_time(datetime.fromisoformat(raw_pickup_at))
    except ValueError:
        return jsonify(ok=False, error="The pickup time is invalid."), 400
    if pickup_at.date() != selected_date:
        return jsonify(ok=False, error="The pickup time is on another day."), 400
    current_service_time = _now().replace(tzinfo=None)
    if selected_date == current_service_time.date() and pickup_at < current_service_time:
        return jsonify(ok=False, error="Choose a pickup time that has not passed."), 400

    configuration = load_configuration(database_path)
    allowed_slots = {
        _local_service_time(value)
        for value in configuration.pickup_times(selected_date)
    }
    if pickup_at not in allowed_slots:
        return jsonify(
            ok=False,
            error="Choose one of the configured service slots.",
        ), 400

    original_pickup_at = _local_service_time(order.pickup_at)
    if pickup_at == original_pickup_at:
        delete_order_slot_assignment(database_path, selected_date, order_id)
        overridden = False
    else:
        save_order_slot_assignment(
            database_path, selected_date, order_id, pickup_at
        )
        overridden = True

    return jsonify(
        ok=True,
        overridden=overridden,
        pickup_at=pickup_at.isoformat(),
        board_content_revision=load_board_content_revision(
            database_path, selected_date
        ),
    )


def _pie_state_payload(state) -> dict[str, object]:
    return {
        "timer_status": state.timer_status,
        "timer_remaining_ms": state.timer_remaining_ms,
        "timer_end_at_ms": state.timer_end_at_ms,
        "oven_position": state.oven_position,
        "updated_at": state.updated_at.isoformat(),
    }


def _live_production_payload(selected_date: date) -> dict[str, object]:
    database_path = _database_path()
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return {
        "ok": True,
        "server_now_ms": int(datetime.now(UTC).timestamp() * 1000),
        "pies": {
            pie_key: _pie_state_payload(state)
            for pie_key, state in load_pie_production_states(
                database_path, selected_date
            ).items()
        },
        "boxed_orders": {
            order_id: boxed_at.astimezone(timezone).isoformat()
            for order_id, boxed_at in load_order_ready_states(
                database_path, selected_date
            ).items()
        },
        "board_content_revision": load_board_content_revision(
            database_path, selected_date
        ),
    }


@blueprint.get("/live-production-state")
def live_production_state():
    selected_date = _parse_service_date(request.args.get("date"))
    return jsonify(_live_production_payload(selected_date))


@blueprint.post("/pie-production-state")
def update_shared_pie_state():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    pie_key = str(payload.get("pie_key", "")).strip()
    if not pie_key or len(pie_key) > 500:
        return jsonify(ok=False, error="A valid pie key is required."), 400
    if not pie_key.startswith(f"{selected_date.isoformat()}|"):
        return jsonify(ok=False, error="The pie key is for another service date."), 400
    action = str(payload.get("timer_action", "")).strip().lower() or None
    try:
        duration_ms = int(payload.get("duration_ms", 480_000))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="The timer duration is invalid."), 400
    kwargs: dict[str, object] = {
        "timer_action": action,
        "duration_ms": duration_ms,
    }
    if "oven_position" in payload:
        raw_position = payload.get("oven_position")
        kwargs["oven_position"] = (
            str(raw_position).strip() if raw_position not in (None, "") else None
        )
    try:
        update_pie_production_state(
            _database_path(), selected_date, pie_key, **kwargs
        )
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(_live_production_payload(selected_date))


@blueprint.post("/order-ready")
def update_order_ready():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400
    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400
    database_path = _database_path()
    if load_order_for_date(database_path, selected_date, order_id) is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    boxed_at = save_order_ready_state(
        database_path, selected_date, order_id, boxed=bool(payload.get("boxed"))
    )
    timezone = ZoneInfo(current_app.config["SERVICE_TIMEZONE"])
    return jsonify(
        ok=True,
        boxed_at=(boxed_at.astimezone(timezone).isoformat() if boxed_at else None),
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


@blueprint.post("/order-remove-unpaid")
def remove_unpaid_square_order():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    database_path = _database_path()
    order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The cached order no longer exists."), 404
    if order.is_walk_in:
        return jsonify(ok=False, error="Walk-in orders cannot be removed with this action."), 400
    if not order.square_order_id:
        return jsonify(ok=False, error="This order is not linked to Square."), 400

    try:
        client = SquareClient(SquareSettings.from_mapping(current_app.config))
        updated_raw = client.cancel_unpaid_order(order.square_order_id)
    except SquareAPIError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    except SquareError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    merge_result = merge_orders_for_date(
        database_path,
        selected_date,
        (),
        candidate_square_order_ids=(order.square_order_id,),
        source="square",
        synced_at=datetime.now(UTC),
    )
    return jsonify(
        ok=True,
        order_state=str(updated_raw.get("state", "")).upper() or "CANCELED",
        removed_count=merge_result.removed_count,
        board_content_revision=load_board_content_revision(
            database_path, selected_date
        ),
    )


@blueprint.post("/order-remove-local")
def remove_local_dashboard_order():
    """Remove an order from this dashboard only; never mutate Square."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return jsonify(ok=False, error="Expected a JSON request."), 400

    selected_date = _parse_service_date(str(payload.get("service_date", "")))
    order_id = str(payload.get("order_id", "")).strip()
    if not order_id:
        return jsonify(ok=False, error="No cached order ID was supplied."), 400

    database_path = _database_path()
    order = load_order_for_date(database_path, selected_date, order_id)
    if order is None:
        return jsonify(ok=False, error="The order is no longer present on this dashboard."), 404

    removal_type = remove_order_from_dashboard(
        database_path, selected_date, order_id
    )
    if removal_type is None:
        return jsonify(ok=False, error="The order is no longer present on this dashboard."), 404

    return jsonify(
        ok=True,
        removal_type=removal_type,
        square_unchanged=True,
        board_content_revision=load_board_content_revision(
            database_path, selected_date
        ),
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
        current_service_time = _now().replace(tzinfo=None)
        if selected_date == current_service_time.date() and pickup_at < current_service_time:
            return jsonify(ok=False, error="Choose a pickup time that has not passed."), 400

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
    return jsonify(
        ok=True,
        board_content_revision=load_board_content_revision(
            _database_path(), selected_date
        ),
    )


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
    client_board_revision = str(payload.get("board_content_revision", ""))
    if selected_date < _now().date():
        return jsonify(
            ok=False,
            error="Refreshes are available only for today or a future service date.",
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
        board_content_revision=(
            current_board_revision := load_board_content_revision(
                _database_path(), selected_date
            )
        ),
        board_content_changed=(current_board_revision != client_board_revision),
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
