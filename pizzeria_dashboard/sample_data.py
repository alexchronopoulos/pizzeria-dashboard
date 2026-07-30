from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    quantity: int
    category: str

    @property
    def pizza_units(self) -> int:
        return self.quantity if self.category == "pizza" else 0


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_name: str
    pickup_at: datetime
    items: tuple[Item, ...]
    released: bool = False

    @property
    def pizza_units(self) -> int:
        return sum(item.pizza_units for item in self.items)

    @property
    def salad_count(self) -> int:
        return sum(item.quantity for item in self.items if item.category == "salad")

    @property
    def drink_count(self) -> int:
        return sum(item.quantity for item in self.items if item.category == "drink")

    @property
    def is_release_candidate(self) -> bool:
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


@dataclass(frozen=True, slots=True)
class ServiceBoard:
    service_date: date
    windows: tuple[PickupWindow, ...]
    pizza_capacity_per_window: int

    @property
    def total_orders(self) -> int:
        return sum(window.order_count for window in self.windows)

    @property
    def total_pizzas(self) -> int:
        return sum(window.pizza_units for window in self.windows)

    @property
    def total_salads(self) -> int:
        return sum(order.salad_count for window in self.windows for order in window.orders)

    @property
    def total_drinks(self) -> int:
        return sum(order.drink_count for window in self.windows for order in window.orders)

    @property
    def release_candidates(self) -> int:
        return sum(
            1
            for window in self.windows
            for order in window.orders
            if order.is_release_candidate
        )


def _service_datetime(hour: int, minute: int) -> datetime:
    # Keep fake data deterministic and easy to recognize in screenshots/tests.
    return datetime.combine(date(2026, 7, 31), time(hour, minute))


def _group_orders(orders: Iterable[Order]) -> tuple[PickupWindow, ...]:
    grouped: dict[datetime, list[Order]] = defaultdict(list)
    for order in orders:
        grouped[order.pickup_at].append(order)

    return tuple(
        PickupWindow(pickup_at=pickup_at, orders=tuple(grouped[pickup_at]))
        for pickup_at in sorted(grouped)
    )


def build_sample_service() -> ServiceBoard:
    orders = (
        Order(
            order_id="PM-1042",
            customer_name="Alex R.",
            pickup_at=_service_datetime(16, 0),
            items=(
                Item("Tomato Pie", 1, "pizza"),
                Item("Mexican Coke", 2, "drink"),
            ),
        ),
        Order(
            order_id="PM-1043",
            customer_name="Maya T.",
            pickup_at=_service_datetime(16, 15),
            items=(
                Item("Plain Pie", 1, "pizza"),
                Item("White Pie", 1, "pizza"),
                Item("Little Gem Salad", 1, "salad"),
            ),
        ),
        Order(
            order_id="PM-1044",
            customer_name="Jordan K.",
            pickup_at=_service_datetime(16, 30),
            items=(Item("Weekly Special", 1, "pizza"),),
        ),
        Order(
            order_id="PM-1045",
            customer_name="Sam D.",
            pickup_at=_service_datetime(16, 45),
            items=(
                Item("Plain Pie", 2, "pizza"),
                Item("Sparkling Water", 1, "drink"),
            ),
        ),
        Order(
            order_id="PM-1046",
            customer_name="Chris M.",
            pickup_at=_service_datetime(17, 0),
            items=(
                Item("White Pie", 1, "pizza"),
                Item("Little Gem Salad", 2, "salad"),
            ),
            released=True,
        ),
        Order(
            order_id="PM-1047",
            customer_name="Taylor B.",
            pickup_at=_service_datetime(17, 15),
            items=(
                Item("Tomato Pie", 1, "pizza"),
                Item("Weekly Special", 1, "pizza"),
            ),
        ),
    )

    return ServiceBoard(
        service_date=orders[0].pickup_at.date(),
        windows=_group_orders(orders),
        pizza_capacity_per_window=2,
    )
