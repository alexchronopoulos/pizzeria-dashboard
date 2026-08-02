from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping

from .domain import Item, Order


@dataclass(frozen=True, slots=True)
class CustomerHistoryOrder:
    """A privacy-minimized, rebuildable summary of one Square order."""

    customer_id: str
    order_id: str
    ordered_at: datetime
    service_date: date
    source: str | None
    items: tuple[Item, ...]

    @property
    def display_items(self) -> tuple[Item, ...]:
        """Food items useful for hospitality context; drinks stay hidden."""
        return tuple(item for item in self.items if item.category != "drink")


@dataclass(frozen=True, slots=True)
class CustomerSummary:
    customer_id: str
    order_count: int
    first_order_at: datetime
    last_order_at: datetime

    @property
    def tag_label(self) -> str:
        if self.order_count <= 1:
            return "First Timer"
        if self.order_count < 5:
            return f"{_ordinal(self.order_count)} Order"
        noun = "order" if self.order_count == 1 else "orders"
        return f"Regular · {self.order_count} {noun}"

    @property
    def tag_kind(self) -> str:
        if self.order_count <= 1:
            return "first-timer"
        if self.order_count < 5:
            return "returning"
        return "regular"


@dataclass(frozen=True, slots=True)
class CustomerVisitSummary:
    """One-day mix of scheduled customers by visit number."""

    total_orders: int
    first_timers: int
    returning: int
    regulars: int
    unavailable: int

    @property
    def matched_orders(self) -> int:
        return self.total_orders - self.unavailable

    def percent(self, value: int) -> int:
        if self.total_orders <= 0:
            return 0
        return round((value / self.total_orders) * 100)


def build_customer_visit_summary(
    orders: Iterable[Order],
    summaries: Mapping[str, CustomerSummary],
) -> CustomerVisitSummary:
    """Classify scheduled orders using their visit count at that order.

    Walk-ins are intentionally excluded because the production workflow does not
    reliably identify those customers. Orders without a Square customer link are
    shown as unavailable so the card also communicates history coverage.
    """
    total_orders = 0
    first_timers = 0
    returning = 0
    regulars = 0
    unavailable = 0

    for order in orders:
        if order.is_walk_in:
            continue
        total_orders += 1
        key = order.square_order_id or order.order_id
        summary = summaries.get(key)
        if summary is None:
            unavailable += 1
        elif summary.order_count <= 1:
            first_timers += 1
        elif summary.order_count < 5:
            returning += 1
        else:
            regulars += 1

    return CustomerVisitSummary(
        total_orders=total_orders,
        first_timers=first_timers,
        returning=returning,
        regulars=regulars,
        unavailable=unavailable,
    )


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    summary: CustomerSummary
    orders: tuple[CustomerHistoryOrder, ...]

    @property
    def recent_orders(self) -> tuple[CustomerHistoryOrder, ...]:
        return self.orders[:5]

    @property
    def older_orders(self) -> tuple[CustomerHistoryOrder, ...]:
        return self.orders[5:]


@dataclass(frozen=True, slots=True)
class CustomerHistorySyncInfo:
    synced_at: datetime
    start_at: datetime
    payment_count: int
    order_count: int


@dataclass(frozen=True, slots=True)
class CustomerHistorySyncResult:
    info: CustomerHistorySyncInfo
    changed_count: int
    warnings: tuple[str, ...] = ()
    incremental: bool = False


def _ordinal(value: int) -> str:
    remainder_100 = value % 100
    if 11 <= remainder_100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def history_order_source(raw_source_name: str | None, raw_source_product: str | None) -> str | None:
    """Return one compact source label without storing contact information."""
    for value in (raw_source_name, raw_source_product):
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned
    return None
