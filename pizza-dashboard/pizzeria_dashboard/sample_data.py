from __future__ import annotations

from datetime import date, datetime, time

from .domain import Item, Modifier, Order, ServiceBoard, build_service_board


def _service_datetime(service_date: date, hour: int, minute: int) -> datetime:
    return datetime.combine(service_date, time(hour, minute))


def _sample_id(service_date: date, order_number: str) -> str:
    # Real Square order IDs are globally unique. Including the date gives the
    # sample source the same property when several service dates are cached.
    return f"sample-{service_date.isoformat()}-{order_number}"


def build_sample_orders(service_date: date | None = None) -> tuple[Order, ...]:
    selected_date = service_date or date(2026, 7, 31)

    def order_id(number: str) -> str:
        return _sample_id(selected_date, number)

    return (
        Order(
            order_id=order_id("PM-1042"),
            customer_name="Alex R.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(Item("Tomato Pie", 1, "pizza"), Item("Mexican Coke", 2, "drink")),
            receipt_number="FCMu",
        ),
        Order(
            order_id=order_id("PM-1043"),
            customer_name="Robin S.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(
                Item(
                    "Plain Pie",
                    1,
                    "pizza",
                    modifiers=(
                        Modifier("Pepperoni"),
                        Modifier("No garlic"),
                        Modifier("Cookie", "cookie", quantity=2),
                    ),
                ),
            ),
            receipt_number="A8pQ",
        ),
        Order(
            order_id=order_id("PM-1044"),
            customer_name="Priya N.",
            pickup_at=_service_datetime(selected_date, 16, 0),
            items=(
                Item(
                    "Weekly Special",
                    1,
                    "pizza",
                    modifiers=(
                        Modifier("Cucumber Salad", "salad"),
                        Modifier("Side Hot Honey", "side"),
                    ),
                ),
            ),
        ),
        Order(
            order_id=order_id("PM-1045"),
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
                    modifiers=(
                        Modifier("Pickled chiles"),
                        Modifier("Basil"),
                        Modifier("Don't cut"),
                        Modifier("Double Cut"),
                        Modifier("Side Ranch", "side"),
                    ),
                ),
            ),
        ),
        Order(
            order_id=order_id("PM-1046"),
            customer_name="Lee C.",
            pickup_at=_service_datetime(selected_date, 16, 15),
            items=(Item("Tomato Pie", 1, "pizza"), Item("Sparkling Water", 1, "drink")),
        ),
        Order(
            order_id=order_id("PM-1047"),
            customer_name="Jordan K.",
            pickup_at=_service_datetime(selected_date, 16, 30),
            items=(Item("Weekly Special", 1, "pizza"),),
        ),
        Order(
            order_id=order_id("PM-1048"),
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
            order_id=order_id("PM-1050"),
            customer_name="Sam D.",
            pickup_at=_service_datetime(selected_date, 16, 45),
            items=(Item("Plain Pie", 2, "pizza"),),
        ),
        Order(
            order_id=order_id("PM-1051"),
            customer_name="Casey W.",
            pickup_at=_service_datetime(selected_date, 16, 45),
            items=(Item("Weekly Special", 1, "pizza"),),
        ),
        Order(
            order_id=order_id("PM-1052"),
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
            order_id=order_id("PM-1053"),
            customer_name="Dana A.",
            pickup_at=_service_datetime(selected_date, 17, 0),
            items=(Item("Tomato Pie", 1, "pizza"),),
        ),
        Order(
            order_id=order_id("PM-1054"),
            customer_name="Taylor B.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(Item("Tomato Pie", 1, "pizza"), Item("Weekly Special", 1, "pizza")),
        ),
        Order(
            order_id=order_id("PM-1055"),
            customer_name="Avery L.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(Item("Plain Pie", 1, "pizza"),),
        ),
        # This order proves that drink-only orders stay out of the production board.
        Order(
            order_id=order_id("PM-1056"),
            customer_name="Jamie Q.",
            pickup_at=_service_datetime(selected_date, 17, 15),
            items=(Item("Mexican Coke", 2, "drink"),),
        ),
    )


def build_sample_service(service_date: date | None = None) -> ServiceBoard:
    selected_date = service_date or date(2026, 7, 31)
    return build_service_board(selected_date, build_sample_orders(selected_date))
