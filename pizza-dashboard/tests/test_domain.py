from datetime import date, datetime
from zoneinfo import ZoneInfo

from pizzeria_dashboard.domain import (
    Item,
    Modifier,
    Order,
    build_service_board,
    production_display_name,
)


def test_board_groups_aware_square_order_with_naive_configured_slot() -> None:
    service_date = date(2026, 7, 31)
    configured_slot = datetime(2026, 7, 31, 16, 0)
    square_pickup = datetime(2026, 7, 31, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    order = Order(
        order_id="square-order-1",
        customer_name="Alex",
        pickup_at=square_pickup,
        items=(Item("Plain Pie", 1, "pizza"),),
    )

    board = build_service_board(
        service_date,
        (order,),
        pickup_times=(configured_slot,),
    )

    assert len(board.windows) == 1
    assert board.windows[0].pickup_at == configured_slot
    assert board.windows[0].orders == (order,)


def test_compact_production_modifiers_hide_salads_and_cookies() -> None:
    item = Item(
        "Plain Pie",
        1,
        "pizza",
        modifiers=(
            Modifier("Pepperoni"),
            Modifier("Cucumber Salad", "salad"),
            Modifier("Cookie", "cookie"),
            Modifier("Double Cut"),
        ),
    )

    assert [modifier.name for modifier in item.production_modifiers] == [
        "Pepperoni",
        "Double Cut",
    ]
    assert item.production_modifiers[-1].is_removal is True


def test_side_modifiers_are_summarized_and_hidden_from_production_modifiers() -> None:
    from datetime import datetime

    from pizzeria_dashboard.domain import Item, Modifier, Order

    item = Item(
        "Plain Pie",
        1,
        "pizza",
        modifiers=(
            Modifier("Side Ranch", "side", 2),
            Modifier("Side Hot Honey", "side", 1),
            Modifier("Pepperoni"),
        ),
    )
    order = Order(
        "order-1",
        "Alex",
        datetime(2026, 7, 31, 16, 0),
        (item,),
    )

    assert order.side_summary == (("Side Hot Honey", 1), ("Side Ranch", 2))
    assert [modifier.name for modifier in item.production_modifiers] == ["Pepperoni"]


def test_production_display_name_strips_only_trailing_parenthetical_copy() -> None:
    assert production_display_name(
        "Spring Beet Salad (local beets, butterhead, etc.)"
    ) == "Spring Beet Salad"
    assert production_display_name("Plain Pie") == "Plain Pie"
    assert production_display_name("Pie (red) with basil") == "Pie (red) with basil"


def test_salad_and_side_summaries_merge_clean_display_names() -> None:
    order = Order(
        "order-2",
        "Alex",
        datetime(2026, 7, 31, 16, 0),
        (
            Item(
                "Plain Pie",
                1,
                "pizza",
                modifiers=(
                    Modifier("Spring Salad (local beets)", "salad", 1),
                    Modifier("Spring Salad (updated description)", "salad", 2),
                    Modifier("Side Ranch (2 oz)", "side", 2),
                ),
            ),
        ),
    )

    assert order.salad_summary == (("Spring Salad", 3),)
    assert order.side_summary == (("Side Ranch", 2),)


def test_walk_ins_stay_unscheduled_until_locally_assigned() -> None:
    service_date = date(2026, 7, 31)
    slot = datetime(2026, 7, 31, 16, 15)
    walk_in = Order(
        "walk-in-1",
        "Ticket 42",
        datetime(2026, 7, 31, 13, 7, tzinfo=ZoneInfo("America/New_York")),
        (Item("Plain Pie", 2, "pizza"),),
        is_walk_in=True,
    )

    unscheduled = build_service_board(
        service_date,
        (walk_in,),
        pickup_times=(slot,),
    )
    assert unscheduled.unscheduled_orders == (walk_in,)
    assert unscheduled.windows[0].orders == ()
    assert unscheduled.total_pizzas == 2

    assigned = build_service_board(
        service_date,
        (walk_in,),
        pickup_times=(slot,),
        walk_in_assignments={"walk-in-1": slot},
    )
    assert assigned.unscheduled_orders == ()
    assert assigned.windows[0].orders == (walk_in,)
    assert assigned.windows[0].pizza_units == 2
