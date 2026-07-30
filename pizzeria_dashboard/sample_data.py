from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable


SALAD_TYPES = (
    "Cucumber Salad",
    "Kale Caesar Salad",
)


@dataclass(frozen=True, slots=True)
class Modifier:
    name: str
    category: str = "topping"
    quantity: int = 1

    @property
    def is_salad(self) -> bool:
        return self.category == "salad"


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    quantity: int
    category: str
    modifiers: tuple[Modifier, ...] = ()

    @property
    def pizza_units(self) -> int:
        return self.quantity if self.category == "pizza" else 0

    @property
    def salad_counts(self) -> Counter[str]:
        """Return side-salad quantities attached to this pizza line.

        Modifier quantities are treated as explicit totals. The future Square
        adapter will normalize Square's modifier representation into this shape.
        """
        counts: Counter[str] = Counter()
        for modifier in self.modifiers:
            if modifier.is_salad:
                counts[modifier.name] += modifier.quantity
        return counts


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_name: str
    pickup_at: datetime
    items: tuple[Item, ...]
    released: bool = False

    @property
    def production_items(self) -> tuple[Item, ...]:
        """Items the kitchen dashboard should display.

        Drinks remain in the source order data but are intentionally hidden from
        this production-focused view.
        """
        return tuple(item for item in self.items if item.category != "drink")

    @property
    def pizza_units(self) -> int:
        return sum(item.pizza_units for item in self.items)

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
    def salad_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for window in self.windows:
            for order in window.orders:
                counts.update(order.salad_counts)
        return counts

    @property
    def total_salads(self) -> int:
        return sum(self.salad_counts.values())

    def is_release_candidate(self, order: Order, window: PickupWindow) -> bool:
        """Return whether a one-pie order can safely release Square capacity."""
        return (
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



def _service_datetime(service_date: date, hour: int, minute: int) -> datetime:
    return datetime.combine(service_date, time(hour, minute))



def _group_orders(orders: Iterable[Order]) -> tuple[PickupWindow, ...]:
    grouped: dict[datetime, list[Order]] = defaultdict(list)
    for order in orders:
        if not order.production_items:
            continue
        grouped[order.pickup_at].append(order)

    return tuple(
        PickupWindow(pickup_at=pickup_at, orders=tuple(grouped[pickup_at]))
        for pickup_at in sorted(grouped)
    )



def build_sample_service(service_date: date | None = None) -> ServiceBoard:
    """Return realistic sample data for the selected service date.

    The date parameter mirrors the date range that will eventually be passed to
    Square. Sample order contents stay the same while the pickup date changes.
    """
    selected_date = service_date or date(2026, 7, 31)
    orders = (
        Order(
            order_id="PM-1042",
            customer_name="Alex R.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(
                Item("Tomato Pie", 1, "pizza"),
                Item("Mexican Coke", 2, "drink"),
            ),
        ),
        Order(
            order_id="PM-1043",
            customer_name="Robin S.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(
                Item(
                    "Plain Pie",
                    1,
                    "pizza",
                    modifiers=(Modifier("Pepperoni"),),
                ),
            ),
        ),
        Order(
            order_id="PM-1044",
            customer_name="Priya N.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(
                Item(
                    "Weekly Special",
                    1,
                    "pizza",
                    modifiers=(Modifier("Cucumber Salad", "salad"),),
                ),
            ),
        ),
        Order(
            order_id="PM-1045",
            customer_name="Maya T.",
            pickup_at=_service_datetime(selected_date, 16, 15),
            items=(
                Item(
                    "Plain Pie",
                    1,
                    "pizza",
                    modifiers=(Modifier("Kale Caesar Salad", "salad"),),
                ),
                Item(
                    "White Pie",
                    1,
                    "pizza",
                    modifiers=(Modifier("Pickled chiles"), Modifier("Basil")),
                ),
            ),
        ),
        Order(
            order_id="PM-1046",
            customer_name="Lee C.",
            pickup_at=_service_datetime(selected_date, 16, 15),
            items=(
                Item("Tomato Pie", 1, "pizza"),
                Item("Sparkling Water", 1, "drink"),
            ),
        ),
        Order(
            order_id="PM-1047",
            customer_name="Jordan K.",
            pickup_at=_service_datetime(selected_date, 16, 30),
            items=(Item("Weekly Special", 1, "pizza"),),
        ),
        Order(
            order_id="PM-1048",
            customer_name="Morgan F.",
            pickup_at=_service_datetime(selected_date, 16, 30),
            items=(
                Item(
                    "White Pie",
                    1,
                    "pizza",
                    modifiers=(
                        Modifier("Basil"),
                        Modifier("Cucumber Salad", "salad", quantity=2),
                    ),
                ),
            ),
            released=True,
        ),
        Order(
            order_id="PM-1050",
            customer_name="Sam D.",
            pickup_at=_service_datetime(selected_date, 16, 45),
            items=(Item("Plain Pie", 2, "pizza"),),
        ),
        Order(
            order_id="PM-1051",
            customer_name="Casey W.",
            pickup_at=_service_datetime(selected_date, 16, 45),
            items=(Item("Weekly Special", 1, "pizza"),),
        ),
        Order(
            order_id="PM-1052",
            customer_name="Chris M.",
            pickup_at=_service_datetime(selected_date, 17, 0),
            items=(
                Item(
                    "White Pie",
                    1,
                    "pizza",
                    modifiers=(Modifier("Kale Caesar Salad", "salad", quantity=2),),
                ),
            ),
            released=True,
        ),
        Order(
            order_id="PM-1053",
            customer_name="Dana A.",
            pickup_at=_service_datetime(selected_date, 17, 0),
            items=(Item("Tomato Pie", 1, "pizza"),),
        ),
        Order(
            order_id="PM-1054",
            customer_name="Taylor B.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(
                Item("Tomato Pie", 1, "pizza"),
                Item("Weekly Special", 1, "pizza"),
            ),
        ),
        Order(
            order_id="PM-1055",
            customer_name="Avery L.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(Item("Plain Pie", 1, "pizza"),),
        ),
        # This order proves that drink-only orders stay out of the production board.
        Order(
            order_id="PM-1056",
            customer_name="Jamie Q.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(Item("Mexican Coke", 2, "drink"),),
        ),
    )

    return ServiceBoard(
        service_date=orders[0].pickup_at.date(),
        windows=_group_orders(orders),
        pizza_capacity_per_window=3,
    )
