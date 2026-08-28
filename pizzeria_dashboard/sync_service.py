from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Mapping

from .database import (
    SyncInfo,
    link_manual_order_square_payment,
    load_linked_manual_square_order_ids,
    load_manual_payment_matches_for_date,
    load_order_for_date,
    load_sync_info,
    merge_orders_for_date,
    replace_orders_for_date,
)
from .domain import Order
from .manual_payments import normalized_ticket_words, ticket_name_contains_match_token
from .sample_data import build_sample_orders
from .square_api import SquareClient, SquareConfigurationError, SquareSettings
from .square_orders import (
    ClassificationRules,
    SquareOrderPull,
    pull_square_orders_for_date,
)


@dataclass(frozen=True, slots=True)
class SyncResult:
    info: SyncInfo
    warnings: tuple[str, ...] = ()
    candidates_scanned: int | None = None
    location_name: str | None = None
    incremental: bool = False
    changed_count: int = 0
    removed_count: int = 0
    reconciled_count: int = 0


def configured_order_source(config: Mapping[str, object]) -> str:
    requested = str(config.get("ORDER_SOURCE", "auto")).strip().lower()
    if requested not in {"auto", "sample", "square"}:
        raise SquareConfigurationError(
            "ORDER_SOURCE must be 'auto', 'sample', or 'square'."
        )
    if requested == "auto":
        return "square" if str(config.get("SQUARE_ACCESS_TOKEN", "")).strip() else "sample"
    return requested


def _rfc3339_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _order_item_signature(order: Order) -> tuple[tuple[object, ...], ...]:
    """Return a catalog-ID-independent item signature for discrepancy warnings."""
    rows: list[tuple[object, ...]] = []
    for item in order.items:
        modifier_rows = tuple(
            sorted(
                (
                    " ".join(normalized_ticket_words(modifier.display_name)),
                    modifier.quantity,
                )
                for modifier in item.modifiers
            )
        )
        rows.append(
            (
                " ".join(normalized_ticket_words(item.display_name)),
                item.category,
                item.quantity,
                modifier_rows,
            )
        )
    return tuple(sorted(rows))


def _reconcile_manual_square_payments(
    path: Path,
    service_date: date,
    orders: tuple[Order, ...],
) -> tuple[tuple[Order, ...], tuple[str, ...], int]:
    """Link exact Ticket Name tokens and remove their duplicate Square cards."""
    payment_matches = load_manual_payment_matches_for_date(path, service_date)
    linked_square_order_ids = load_linked_manual_square_order_ids(
        path, service_date
    )
    pending_matches = tuple(
        match for match in payment_matches.values() if not match.paid_in_square
    )
    visible_orders: list[Order] = []
    warnings: list[str] = []
    reconciled_count = 0

    for order in orders:
        square_order_id = str(order.square_order_id or "").strip()
        if square_order_id and square_order_id in linked_square_order_ids:
            continue

        is_completed_walk_in = bool(
            order.is_walk_in
            and square_order_id
            and str(order.square_order_state or "").upper() == "COMPLETED"
            and order.ticket_name
        )
        if not is_completed_walk_in:
            visible_orders.append(order)
            continue

        token_matches = tuple(
            match
            for match in pending_matches
            if ticket_name_contains_match_token(
                order.ticket_name,
                match.match_token,
            )
        )
        if len(token_matches) != 1:
            if len(token_matches) > 1:
                warnings.append(
                    f"Square order {square_order_id} contains more than one manual-order payment name and was not linked."
                )
            visible_orders.append(order)
            continue

        match = token_matches[0]
        manual_order = load_order_for_date(
            path,
            service_date,
            match.manual_order_id,
        )
        if manual_order is None or not manual_order.is_manual:
            visible_orders.append(order)
            continue
        item_discrepancy = (
            _order_item_signature(manual_order) != _order_item_signature(order)
        )
        linked = link_manual_order_square_payment(
            path,
            service_date,
            match.manual_order_id,
            square_order_id=square_order_id,
            square_receipt_number=order.receipt_number,
            square_ticket_name=order.ticket_name,
            item_discrepancy=item_discrepancy,
        )
        if not linked:
            visible_orders.append(order)
            continue

        linked_square_order_ids.add(square_order_id)
        reconciled_count += 1
        if item_discrepancy:
            warnings.append(
                f"{match.match_token} was marked Paid in Square, but its Square items differ from the manual order."
            )

    return tuple(visible_orders), tuple(warnings), reconciled_count


def sync_orders_for_date(
    path: Path,
    service_date: date,
    config: Mapping[str, object],
    *,
    square_client: SquareClient | None = None,
    incremental: bool = False,
) -> SyncResult:
    source = configured_order_source(config)
    if source == "sample":
        return SyncResult(
            info=replace_orders_for_date(
                path,
                service_date,
                build_sample_orders(service_date),
                source="sample",
            )
        )

    settings = SquareSettings.from_mapping(config)
    client = square_client or SquareClient(settings)
    location = client.resolve_location()
    location_id = str(location.get("id", "")).strip()
    if not location_id:
        raise SquareConfigurationError("The selected Square location has no ID.")

    # Capture the cursor before the API call starts. The next incremental pull
    # overlaps this point, so an order updated while the request is in flight
    # cannot fall into a gap between refreshes.
    sync_started_at = datetime.now(UTC)
    previous_sync = load_sync_info(path, service_date)
    use_incremental = incremental and previous_sync is not None

    updated_start_at: str | None = None
    updated_end_at: str | None = None
    if use_incremental:
        try:
            overlap_seconds = int(
                config.get("SQUARE_INCREMENTAL_OVERLAP_SECONDS", 120)
            )
        except (TypeError, ValueError):
            overlap_seconds = 120
        overlap_seconds = max(30, min(overlap_seconds, 900))
        updated_start_at = _rfc3339_utc(
            previous_sync.synced_at - timedelta(seconds=overlap_seconds)
        )
        updated_end_at = _rfc3339_utc(sync_started_at + timedelta(seconds=5))

    pull: SquareOrderPull = pull_square_orders_for_date(
        client,
        service_date=service_date,
        timezone_name=str(config.get("SERVICE_TIMEZONE", "America/New_York")),
        location_id=location_id,
        lookback_days=settings.order_lookback_days,
        rules=ClassificationRules.from_mapping(config),
        updated_start_at=updated_start_at,
        updated_end_at=updated_end_at,
    )
    visible_orders, reconciliation_warnings, reconciled_count = (
        _reconcile_manual_square_payments(
            path,
            service_date,
            pull.orders,
        )
    )

    changed_count = 0
    removed_count = 0
    if use_incremental:
        merge = merge_orders_for_date(
            path,
            service_date,
            visible_orders,
            candidate_square_order_ids=pull.candidate_square_order_ids,
            source="square",
            synced_at=sync_started_at,
        )
        info = merge.info
        changed_count = merge.changed_count
        removed_count = merge.removed_count
    else:
        info = replace_orders_for_date(
            path,
            service_date,
            visible_orders,
            source="square",
            synced_at=sync_started_at,
        )
        changed_count = len(visible_orders)
    changed_count += reconciled_count

    return SyncResult(
        info=info,
        warnings=(*pull.warnings, *reconciliation_warnings),
        candidates_scanned=pull.candidates_scanned,
        location_name=(str(location.get("name")) if location.get("name") else None),
        incremental=use_incremental,
        changed_count=changed_count,
        removed_count=removed_count,
        reconciled_count=reconciled_count,
    )


def sync_sample_orders(path: Path, service_date: date) -> SyncInfo:
    """Compatibility helper retained for tests and sample mode."""
    return replace_orders_for_date(
        path,
        service_date,
        build_sample_orders(service_date),
        source="sample",
    )
