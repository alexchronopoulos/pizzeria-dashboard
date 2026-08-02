from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .customer_history import (
    CustomerHistoryOrder,
    CustomerHistorySyncResult,
    history_order_source,
)
from .database import (
    load_customer_history_sync_info,
    merge_customer_history,
    replace_customer_history,
)
from .square_api import SquareClient, SquareConfigurationError, SquareSettings
from .square_orders import (
    ClassificationRules,
    build_catalog_index,
    build_modifier_index,
    convert_square_order_items,
)


def _rfc3339_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, timezone: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(timezone)


def _configured_start_at(config: Mapping[str, object], timezone: ZoneInfo) -> datetime:
    raw_value = str(config.get("CUSTOMER_HISTORY_START_DATE", "2025-01-01")).strip()
    try:
        start_date = date.fromisoformat(raw_value)
    except ValueError as exc:
        raise SquareConfigurationError(
            "CUSTOMER_HISTORY_START_DATE must use YYYY-MM-DD format."
        ) from exc
    return datetime.combine(start_date, time.min, tzinfo=timezone)


def _payment_order_links(
    payments: tuple[Mapping[str, object], ...],
    timezone: ZoneInfo,
) -> tuple[dict[str, tuple[str, datetime]], tuple[str, ...]]:
    """Return order -> (customer, payment time) links from completed payments."""
    links: dict[str, tuple[str, datetime]] = {}
    warnings: list[str] = []
    conflicted_order_ids: set[str] = set()

    for payment in payments:
        if str(payment.get("status", "")).upper() != "COMPLETED":
            continue
        order_id = str(payment.get("order_id", "")).strip()
        customer_id = str(payment.get("customer_id", "")).strip()
        if not order_id or not customer_id:
            continue
        ordered_at = _parse_datetime(payment.get("created_at"), timezone)
        if ordered_at is None:
            continue

        existing = links.get(order_id)
        if existing is not None and existing[0] != customer_id:
            conflicted_order_ids.add(order_id)
            links.pop(order_id, None)
            continue
        if order_id not in conflicted_order_ids:
            links[order_id] = (customer_id, ordered_at)

    if conflicted_order_ids:
        warnings.append(
            f"Skipped {len(conflicted_order_ids)} split-payment order(s) linked to multiple customer profiles."
        )
    return links, tuple(warnings)


def _source_label(raw_order: Mapping[str, object]) -> str | None:
    creation_source = raw_order.get("creation_source")
    if not isinstance(creation_source, Mapping):
        return None
    return history_order_source(
        str(creation_source.get("name", "")).strip() or None,
        str(creation_source.get("product", "")).strip() or None,
    )


def sync_customer_history(
    path: Path,
    config: Mapping[str, object],
    *,
    square_client: SquareClient | None = None,
    full: bool = False,
    force: bool = False,
) -> CustomerHistorySyncResult | None:
    """Build or incrementally refresh the rebuildable customer-history index.

    Return ``None`` when no initial history exists or an incremental refresh is
    not yet due. Full rebuilds always run when explicitly requested.
    """
    settings = SquareSettings.from_mapping(config)
    client = square_client or SquareClient(settings)
    location = client.resolve_location()
    location_id = str(location.get("id", "")).strip()
    if not location_id:
        raise SquareConfigurationError("The selected Square location has no ID.")

    timezone = ZoneInfo(str(config.get("SERVICE_TIMEZONE", "America/New_York")))
    now = datetime.now(UTC)
    previous = load_customer_history_sync_info(path)
    if not full and previous is None:
        return None

    try:
        minimum_refresh_seconds = int(
            config.get("CUSTOMER_HISTORY_REFRESH_SECONDS", 300)
        )
    except (TypeError, ValueError):
        minimum_refresh_seconds = 300
    minimum_refresh_seconds = max(60, min(minimum_refresh_seconds, 3600))
    if (
        not full
        and not force
        and previous is not None
        and now - previous.synced_at.astimezone(UTC)
        < timedelta(seconds=minimum_refresh_seconds)
    ):
        return None

    configured_start = _configured_start_at(config, timezone)
    if full:
        start_at = configured_start
    else:
        try:
            overlap_hours = int(config.get("CUSTOMER_HISTORY_OVERLAP_HOURS", 48))
        except (TypeError, ValueError):
            overlap_hours = 48
        overlap_hours = max(1, min(overlap_hours, 168))
        start_at = max(
            configured_start,
            previous.synced_at.astimezone(timezone) - timedelta(hours=overlap_hours),
        )

    if full:
        payments = client.list_payments(
            location_id=location_id,
            begin_time=_rfc3339_utc(start_at),
            end_time=_rfc3339_utc(now + timedelta(seconds=5)),
        )
    else:
        payments = client.list_payments(
            location_id=location_id,
            updated_at_begin_time=_rfc3339_utc(start_at),
            updated_at_end_time=_rfc3339_utc(now + timedelta(seconds=5)),
        )
    links, link_warnings = _payment_order_links(payments, timezone)
    raw_orders = client.batch_retrieve_orders(tuple(links), location_id=location_id)
    raw_by_id = {
        str(raw_order.get("id")): raw_order
        for raw_order in raw_orders
        if raw_order.get("id")
    }

    catalog_index = build_catalog_index(client, raw_orders)
    modifier_index = build_modifier_index(client, raw_orders)
    rules = ClassificationRules.from_mapping(config)
    history_orders: list[CustomerHistoryOrder] = []
    skipped_missing = 0

    for order_id, (customer_id, payment_at) in links.items():
        raw_order = raw_by_id.get(order_id)
        if raw_order is None:
            skipped_missing += 1
            continue
        if str(raw_order.get("state", "")).upper() in {"DRAFT", "CANCELED"}:
            continue
        items = convert_square_order_items(
            raw_order,
            catalog_index=catalog_index,
            modifier_index=modifier_index,
            rules=rules,
        )
        history_orders.append(
            CustomerHistoryOrder(
                customer_id=customer_id,
                order_id=order_id,
                ordered_at=payment_at,
                service_date=payment_at.date(),
                source=_source_label(raw_order),
                items=items,
            )
        )

    warnings = list(link_warnings)
    if skipped_missing:
        warnings.append(
            f"Square did not return {skipped_missing} historical order document(s); those payments were skipped."
        )

    if full:
        info = replace_customer_history(
            path,
            history_orders,
            synced_at=now,
            start_at=configured_start,
            payment_count=len(payments),
        )
        changed_count = len(history_orders)
    else:
        info, changed_count = merge_customer_history(
            path,
            history_orders,
            synced_at=now,
            start_at=start_at,
            payment_count=len(payments),
        )

    return CustomerHistorySyncResult(
        info=info,
        changed_count=changed_count,
        warnings=tuple(warnings),
        incremental=not full,
    )
