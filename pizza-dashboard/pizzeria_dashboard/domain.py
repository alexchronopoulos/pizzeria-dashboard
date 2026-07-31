from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping


def production_display_name(name: str) -> str:
    """Return the compact kitchen label used on the production dashboard.

    Square item and modifier names sometimes include a trailing parenthetical
    description. Keep the exact source name in cached data and the order inspector,
    but remove that descriptive suffix from the fast-scanning production view.
    """
    value = str(name).strip()
    compact = re.sub(r"\s+\(.*\)\s*$", "", value).strip()
    return compact or value


@dataclass(frozen=True, slots=True)
class Modifier:
    name: str
    category: str = "topping"
    quantity: int = 1
    catalog_object_id: str | None = None

    @property
    def display_name(self) -> str:
        return production_display_name(self.name)

    @property
    def is_salad(self) -> bool:
        return self.category == "salad" or bool(
            re.search(r"\bsalads?\b", self.name, re.IGNORECASE)
        )

    @property
    def is_side(self) -> bool:
        return self.category == "side" or bool(
            re.search(r"\bside\b", self.name, re.IGNORECASE)
        )

    @property
    def is_cookie(self) -> bool:
        return self.category == "cookie" or bool(
            re.search(r"\bcookies?\b", self.name, re.IGNORECASE)
        )

    @property
    def is_removal(self) -> bool:
        return bool(
            re.search(
                r"\b(?:no|don['’]?t)\b|\bdouble\s+cut\b",
                self.name,
                re.IGNORECASE,
            )
        )


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    quantity: int
    category: str
    modifiers: tuple[Modifier, ...] = ()
    catalog_object_id: str | None = None
    variation_name: str | None = None
    catalog_categories: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return production_display_name(self.name)

    @property
    def pizza_units(self) -> int:
        return self.quantity if self.category == "pizza" else 0

    @property
    def is_cookie(self) -> bool:
        return self.category == "cookie" or "cookie" in self.name.casefold()

    @property
    def cookie_units(self) -> int:
        return self.quantity if self.is_cookie else 0

    @property
    def salad_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for modifier in self.modifiers:
            if modifier.is_salad:
                counts[modifier.name] += modifier.quantity
        return counts

    @property
    def side_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for modifier in self.modifiers:
            if modifier.is_side:
                counts[modifier.name] += modifier.quantity
        return counts

    @property
    def modifier_cookie_units(self) -> int:
        return sum(
            modifier.quantity for modifier in self.modifiers if modifier.is_cookie
        )

    @property
    def production_modifiers(self) -> tuple[Modifier, ...]:
        """Modifiers that need to remain visible beneath the pizza item.

        Salad, side, and cookie modifiers are summarized as order-level badges to
        reduce repetition in the fast-scanning production view. They remain
        available in the cached order document and the full order-details modal.
        """
        return tuple(
            modifier
            for modifier in self.modifiers
            if not modifier.is_salad and not modifier.is_side and not modifier.is_cookie
        )


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_name: str
    pickup_at: datetime
    items: tuple[Item, ...]
    receipt_number: str | None = None
    released: bool = False
    square_order_id: str | None = None
    square_version: int | None = None
    location_id: str | None = None
    fulfillment_uid: str | None = None
    fulfillment_state: str | None = None
    source_updated_at: datetime | None = None
    is_walk_in: bool = False
    source_created_at: datetime | None = None
    source_closed_at: datetime | None = None
    creation_product: str | None = None
    ticket_name: str | None = None

    @property
    def production_items(self) -> tuple[Item, ...]:
        """Items that require attention on the production dashboard."""
        return tuple(item for item in self.items if item.category != "drink")

    @property
    def pizza_units(self) -> int:
        return sum(item.pizza_units for item in self.items)

    @property
    def cookie_count(self) -> int:
        return sum(
            item.cookie_units + item.modifier_cookie_units for item in self.items
        )

    @property
    def has_cookie(self) -> bool:
        return self.cookie_count > 0

    @property
    def salad_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for item in self.items:
            counts.update(item.salad_counts)
        return counts

    @property
    def salad_count(self) -> int:
        return sum(self.salad_counts.values())

    @property
    def has_salad(self) -> bool:
        return self.salad_count > 0

    @property
    def salad_summary(self) -> tuple[tuple[str, int], ...]:
        counts: Counter[str] = Counter()
        for name, quantity in self.salad_counts.items():
            counts[production_display_name(name)] += quantity
        return tuple(sorted(counts.items(), key=lambda entry: entry[0].casefold()))

    @property
    def side_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for item in self.items:
            counts.update(item.side_counts)
        return counts

    @property
    def side_count(self) -> int:
        return sum(self.side_counts.values())

    @property
    def has_side(self) -> bool:
        return self.side_count > 0

    @property
    def side_summary(self) -> tuple[tuple[str, int], ...]:
        counts: Counter[str] = Counter()
        for name, quantity in self.side_counts.items():
            counts[production_display_name(name)] += quantity
        return tuple(sorted(counts.items(), key=lambda entry: entry[0].casefold()))

    @property
    def display_reference(self) -> str | None:
        if self.receipt_number:
            return f"Receipt {self.receipt_number}"
        if self.square_order_id:
            return f"Square {self.square_order_id[-8:]}"
        return None

    @property
    def is_single_pie_unreleased(self) -> bool:
        return self.pizza_units == 1 and not self.released


@dataclass(frozen=True, slots=True)
class PickupWindow:
    pickup_at: datetime
    orders: tuple[Order, ...]

    @property
    def pizza_units(self) -> int:
        return sum(order.pizza_units for order in self.orders)

    @property
    def order_count(self) -> int:
        return len(self.orders)

    @property
    def is_empty(self) -> bool:
        return not self.orders


@dataclass(frozen=True, slots=True)
class ServiceBoard:
    service_date: date
    windows: tuple[PickupWindow, ...]
    pizza_capacity_per_window: int
    unscheduled_orders: tuple[Order, ...] = ()

    @property
    def all_orders(self) -> tuple[Order, ...]:
        return (
            *(order for window in self.windows for order in window.orders),
            *self.unscheduled_orders,
        )

    @property
    def walk_in_orders(self) -> tuple[Order, ...]:
        return tuple(order for order in self.all_orders if order.is_walk_in)

    @property
    def total_orders(self) -> int:
        return len(self.all_orders)

    @property
    def total_pizzas(self) -> int:
        return sum(order.pizza_units for order in self.all_orders)

    @property
    def salad_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for order in self.all_orders:
            counts.update(order.salad_counts)
        return counts

    @property
    def total_salads(self) -> int:
        return sum(self.salad_counts.values())

    @property
    def side_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for order in self.all_orders:
            counts.update(order.side_counts)
        return counts

    @property
    def total_sides(self) -> int:
        return sum(self.side_counts.values())

    @property
    def total_cookies(self) -> int:
        return sum(order.cookie_count for order in self.all_orders)

    def is_release_candidate(self, order: Order, window: PickupWindow) -> bool:
        return (
            not order.is_walk_in
            and
            order.is_single_pie_unreleased
            and window.pizza_units < self.pizza_capacity_per_window
        )

    @property
    def release_candidates(self) -> int:
        return sum(
            1
            for window in self.windows
            for order in window.orders
            if self.is_release_candidate(order, window)
        )

    @property
    def release_candidate_windows(self) -> tuple[PickupWindow, ...]:
        return tuple(
            window
            for window in self.windows
            if any(self.is_release_candidate(order, window) for order in window.orders)
        )

    def open_capacity(self, window: PickupWindow) -> int:
        return max(self.pizza_capacity_per_window - window.pizza_units, 0)

    @property
    def one_pie_available_windows(self) -> tuple[PickupWindow, ...]:
        return tuple(window for window in self.windows if self.open_capacity(window) == 1)

    @property
    def two_pie_available_windows(self) -> tuple[PickupWindow, ...]:
        return tuple(window for window in self.windows if self.open_capacity(window) >= 2)


def _service_wall_time(value: datetime) -> datetime:
    """Return a timezone-neutral local service timestamp.

    Square pickup timestamps are converted to the configured service timezone and
    remain timezone-aware. Configured pickup slots are local wall-clock values and
    are timezone-naive. The production board only compares times within one local
    service date, so normalizing both forms to local wall time makes them safe to
    group and sort together.
    """
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def build_service_board(
    service_date: date,
    orders: Iterable[Order],
    pizza_capacity_per_window: int = 3,
    pickup_times: Iterable[datetime] = (),
    walk_in_assignments: Mapping[str, datetime] | None = None,
) -> ServiceBoard:
    walk_in_assignments = walk_in_assignments or {}
    grouped: dict[datetime, list[Order]] = defaultdict(list)
    unscheduled: list[Order] = []
    for pickup_at in pickup_times:
        grouped[_service_wall_time(pickup_at)]
    for order in orders:
        if not order.production_items:
            continue
        if order.is_walk_in:
            assigned_pickup_at = walk_in_assignments.get(order.order_id)
            if assigned_pickup_at is None:
                unscheduled.append(order)
                continue
            grouped[_service_wall_time(assigned_pickup_at)].append(order)
            continue
        grouped[_service_wall_time(order.pickup_at)].append(order)

    windows = tuple(
        PickupWindow(
            pickup_at=pickup_at,
            orders=tuple(sorted(grouped[pickup_at], key=lambda order: order.order_id)),
        )
        for pickup_at in sorted(grouped)
    )
    return ServiceBoard(
        service_date=service_date,
        windows=windows,
        pizza_capacity_per_window=pizza_capacity_per_window,
        unscheduled_orders=tuple(
            sorted(
                unscheduled,
                key=lambda order: (
                    _service_wall_time(
                        order.source_closed_at
                        or order.source_created_at
                        or order.pickup_at
                    ),
                    order.order_id,
                ),
            )
        ),
    )


def order_to_payload(order: Order) -> dict[str, object]:
    """Serialize an order into a stable document for the SQLite cache."""
    return {
        "order_id": order.order_id,
        "customer_name": order.customer_name,
        "pickup_at": order.pickup_at.isoformat(),
        "receipt_number": order.receipt_number,
        "released": order.released,
        "square_order_id": order.square_order_id,
        "square_version": order.square_version,
        "location_id": order.location_id,
        "fulfillment_uid": order.fulfillment_uid,
        "fulfillment_state": order.fulfillment_state,
        "source_updated_at": (
            order.source_updated_at.isoformat() if order.source_updated_at else None
        ),
        "is_walk_in": order.is_walk_in,
        "source_created_at": (
            order.source_created_at.isoformat() if order.source_created_at else None
        ),
        "source_closed_at": (
            order.source_closed_at.isoformat() if order.source_closed_at else None
        ),
        "creation_product": order.creation_product,
        "ticket_name": order.ticket_name,
        "items": [
            {
                "name": item.name,
                "quantity": item.quantity,
                "category": item.category,
                "catalog_object_id": item.catalog_object_id,
                "variation_name": item.variation_name,
                "catalog_categories": list(item.catalog_categories),
                "modifiers": [
                    {
                        "name": modifier.name,
                        "category": modifier.category,
                        "quantity": modifier.quantity,
                        "catalog_object_id": modifier.catalog_object_id,
                    }
                    for modifier in item.modifiers
                ],
            }
            for item in order.items
        ],
    }


def _positive_int(value: object, fallback: int = 1) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return fallback


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def order_from_payload(payload: Mapping[str, object]) -> Order:
    """Hydrate an order document from SQLite.

    This adapter is deliberately tolerant of extra fields so the cached payload
    can evolve without requiring a database migration for every new attribute.
    """
    raw_items = payload.get("items", [])
    items: list[Item] = []
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue
            raw_modifiers = raw_item.get("modifiers", [])
            modifiers: list[Modifier] = []
            if isinstance(raw_modifiers, list):
                for raw_modifier in raw_modifiers:
                    if not isinstance(raw_modifier, Mapping):
                        continue
                    modifiers.append(
                        Modifier(
                            name=str(raw_modifier.get("name", "Modifier")),
                            category=str(raw_modifier.get("category", "topping")),
                            quantity=_positive_int(raw_modifier.get("quantity")),
                            catalog_object_id=(
                                str(raw_modifier["catalog_object_id"])
                                if raw_modifier.get("catalog_object_id")
                                else None
                            ),
                        )
                    )
            raw_categories = raw_item.get("catalog_categories", [])
            categories = (
                tuple(str(value) for value in raw_categories)
                if isinstance(raw_categories, list)
                else ()
            )
            items.append(
                Item(
                    name=str(raw_item.get("name", "Item")),
                    quantity=_positive_int(raw_item.get("quantity")),
                    category=str(raw_item.get("category", "other")),
                    modifiers=tuple(modifiers),
                    catalog_object_id=(
                        str(raw_item["catalog_object_id"])
                        if raw_item.get("catalog_object_id")
                        else None
                    ),
                    variation_name=(
                        str(raw_item["variation_name"])
                        if raw_item.get("variation_name")
                        else None
                    ),
                    catalog_categories=categories,
                )
            )

    return Order(
        order_id=str(payload["order_id"]),
        customer_name=str(payload.get("customer_name", "Guest")),
        pickup_at=datetime.fromisoformat(str(payload["pickup_at"])),
        receipt_number=(
            str(payload["receipt_number"])
            if payload.get("receipt_number")
            else None
        ),
        items=tuple(items),
        released=bool(payload.get("released", False)),
        square_order_id=(
            str(payload["square_order_id"])
            if payload.get("square_order_id")
            else None
        ),
        square_version=_optional_int(payload.get("square_version")),
        location_id=(
            str(payload["location_id"]) if payload.get("location_id") else None
        ),
        fulfillment_uid=(
            str(payload["fulfillment_uid"])
            if payload.get("fulfillment_uid")
            else None
        ),
        fulfillment_state=(
            str(payload["fulfillment_state"])
            if payload.get("fulfillment_state")
            else None
        ),
        source_updated_at=_optional_datetime(payload.get("source_updated_at")),
        is_walk_in=bool(payload.get("is_walk_in", False)),
        source_created_at=_optional_datetime(payload.get("source_created_at")),
        source_closed_at=_optional_datetime(payload.get("source_closed_at")),
        creation_product=(
            str(payload["creation_product"])
            if payload.get("creation_product")
            else None
        ),
        ticket_name=(
            str(payload["ticket_name"])
            if payload.get("ticket_name")
            else None
        ),
    )
