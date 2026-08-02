from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .domain import Item


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
