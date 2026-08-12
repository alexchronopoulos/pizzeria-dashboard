from datetime import date, datetime
from zoneinfo import ZoneInfo

from pizzeria_dashboard.domain import (
    Item,
    Modifier,
    Order,
    build_service_board,
    customer_display_name,
    parse_ticket_pickup_time,
    production_display_name,
)




def test_customer_display_name_masks_surnames_and_ticket_times() -> None:
    assert customer_display_name("Alex Christopher") == "Alex C."
    assert customer_display_name("Tim S.") == "Tim S."
    assert customer_display_name("Sam 7:45") == "Sam"
    assert customer_display_name("5:45 Peter Johnson") == "Peter J."
    assert customer_display_name("Guest") == "Guest"

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


def test_scheduled_order_uses_local_pickup_override_for_board_capacity() -> None:
    service_date = date(2026, 7, 31)
    original_slot = datetime(2026, 7, 31, 16, 0)
    adjusted_slot = datetime(2026, 7, 31, 16, 15)
    order = Order(
        "scheduled-1",
        "Alex",
        original_slot,
        (Item("Plain Pie", 2, "pizza"),),
    )

    board = build_service_board(
        service_date,
        (order,),
        pickup_times=(original_slot, adjusted_slot),
        pickup_time_overrides={order.order_id: adjusted_slot},
    )

    assert board.windows[0].orders == ()
    assert board.windows[0].pizza_units == 0
    assert board.windows[1].orders == (order,)
    assert board.windows[1].pizza_units == 2


def test_non_pizza_walk_ins_are_hidden_but_mixed_walk_in_remains() -> None:
    service_date = date(2026, 7, 31)
    event_at = datetime(2026, 7, 31, 13, 15, tzinfo=ZoneInfo("America/New_York"))
    slice_only = Order(
        "slice-only",
        "Counter 1",
        event_at,
        (Item("Plain Slice", 2, "slice"),),
        is_walk_in=True,
    )
    cookie_only = Order(
        "cookie-only",
        "Counter cookie",
        event_at,
        (Item("TCHO Miso Chocolate Chip Cookie", 2, "cookie"),),
        is_walk_in=True,
    )
    mixed = Order(
        "mixed",
        "Counter 2",
        event_at,
        (
            Item("Plain Pie", 1, "pizza"),
            Item("Plain Slice", 1, "slice"),
        ),
        is_walk_in=True,
    )

    board = build_service_board(service_date, (slice_only, cookie_only, mixed))

    assert board.unscheduled_orders == (mixed,)
    assert mixed.production_items == (mixed.items[0],)
    assert slice_only.production_items == ()
    assert cookie_only.production_items == cookie_only.items


def test_order_all_day_counts_match_service_summary_rules() -> None:
    order = Order(
        "order-all-day",
        "Alex",
        datetime(2026, 7, 31, 16, 0),
        (
            Item(
                "Plain Pie",
                2,
                "pizza",
                modifiers=(
                    Modifier("Pesto", quantity=1),
                    Modifier("Hot Honey", quantity=2),
                    Modifier("No Basil"),
                    Modifier("Side Ranch", category="side"),
                ),
            ),
            Item("White Pie", 1, "pizza", modifiers=(Modifier("Pesto"),)),
        ),
    )

    assert order.pizza_counts == {"Plain Pie": 2, "White Pie": 1}
    assert order.modifier_counts == {"Pesto": 3, "Hot Honey": 4}


def test_ticket_name_times_resolve_against_configured_slots() -> None:
    service_date = date(2026, 7, 31)
    slots = (
        datetime(2026, 7, 31, 11, 30),
        datetime(2026, 7, 31, 17, 45),
        datetime(2026, 7, 31, 19, 30),
    )

    assert parse_ticket_pickup_time("Sam 7:30", service_date, slots) == slots[2]
    assert parse_ticket_pickup_time("5:45 Peter", service_date, slots) == slots[1]
    assert parse_ticket_pickup_time("11:30 AM Jamie", service_date, slots) == slots[0]
    assert parse_ticket_pickup_time("Sam order 42", service_date, slots) is None
    assert parse_ticket_pickup_time("Sam 7:32", service_date, slots) is None
    assert parse_ticket_pickup_time("Sam — pickup 7:30 PM please", service_date, slots) == slots[2]
    assert parse_ticket_pickup_time("Sam 7：30", service_date, slots) == slots[2]


def test_ticket_name_auto_assigns_walk_in_but_manual_override_wins() -> None:
    service_date = date(2026, 7, 31)
    parsed_slot = datetime(2026, 7, 31, 19, 30)
    manual_slot = datetime(2026, 7, 31, 18, 0)
    walk_in = Order(
        "walk-in-ticket-time",
        "Sam 7:30",
        datetime(2026, 7, 31, 13, 7),
        (Item("Plain Pie", 1, "pizza"),),
        is_walk_in=True,
        ticket_name="Sam 7:30",
    )

    automatic = build_service_board(
        service_date,
        (walk_in,),
        pickup_times=(manual_slot, parsed_slot),
    )
    assert automatic.unscheduled_orders == ()
    assert automatic.windows[1].orders == (walk_in,)

    overridden = build_service_board(
        service_date,
        (walk_in,),
        pickup_times=(manual_slot, parsed_slot),
        walk_in_assignments={walk_in.order_id: manual_slot},
    )
    assert overridden.windows[0].orders == (walk_in,)
    assert overridden.windows[1].orders == ()

    forced_unscheduled = build_service_board(
        service_date,
        (walk_in,),
        pickup_times=(manual_slot, parsed_slot),
        walk_in_assignments={walk_in.order_id: None},
    )
    assert forced_unscheduled.unscheduled_orders == (walk_in,)


def test_walk_in_display_name_keeps_ticket_name_unchanged() -> None:
    order = Order(
        order_id="walk-in-name",
        customer_name="Sam 7:45",
        pickup_at=datetime(2026, 7, 31, 19, 45),
        items=(Item("Plain Pie", 1, "pizza"),),
        is_walk_in=True,
        ticket_name="Sam 7:45",
    )

    assert order.display_customer_name == "Sam 7:45"


def test_pizza_summary_aggregates_whole_pies_across_all_orders() -> None:
    service_date = date(2026, 7, 31)
    scheduled_slot = datetime(2026, 7, 31, 16, 0)
    orders = (
        Order(
            "scheduled-1",
            "Alex",
            scheduled_slot,
            (
                Item("Collar City", 2, "pizza"),
                Item("Plain Slice", 3, "slice"),
            ),
        ),
        Order(
            "scheduled-2",
            "Tim",
            scheduled_slot,
            (Item("Cherry Tomato (local tomatoes)", 1, "pizza"),),
        ),
        Order(
            "walk-in-1",
            "Sam 7:30",
            datetime(2026, 7, 31, 13, 0),
            (
                Item("Collar City", 1, "pizza"),
                Item("Cookie", 2, "cookie"),
            ),
            is_walk_in=True,
            ticket_name="Sam 7:30",
        ),
    )

    board = build_service_board(
        service_date,
        orders,
        pickup_times=(scheduled_slot, datetime(2026, 7, 31, 19, 30)),
    )

    assert board.total_pizzas == 4
    assert board.pizza_summary == (
        ("Collar City", 3),
        ("Cherry Tomato", 1),
    )


def test_pizza_summary_breaks_quantity_ties_by_item_name() -> None:
    service_date = date(2026, 7, 31)
    slot = datetime(2026, 7, 31, 16, 0)
    orders = (
        Order(
            "summary-sort",
            "Alex",
            slot,
            (
                Item("Plain Pie", 2, "pizza"),
                Item("Cherry Tomato", 2, "pizza"),
                Item("Collar City", 3, "pizza"),
            ),
        ),
    )

    board = build_service_board(service_date, orders, pickup_times=(slot,))

    assert board.pizza_summary == (
        ("Collar City", 3),
        ("Cherry Tomato", 2),
        ("Plain Pie", 2),
    )


def test_modifier_summary_counts_pizza_addons_and_excludes_removals() -> None:
    service_date = date(2026, 8, 6)
    order = Order(
        order_id="order-1",
        customer_name="Alex",
        pickup_at=datetime(2026, 8, 6, 16, 0),
        items=(
            Item(
                "Plain Pie",
                2,
                "pizza",
                modifiers=(
                    Modifier("Pesto", quantity=1),
                    Modifier("No Onion", quantity=1),
                    Modifier("Cucumber Salad", category="salad", quantity=1),
                ),
            ),
        ),
    )
    board = build_service_board(
        service_date,
        (order,),
        pickup_times=(datetime(2026, 8, 6, 16, 0),),
    )

    assert board.modifier_counts == {"Pesto": 2}
    assert board.modifier_summary == (("Pesto", 2),)


def test_main_order_salads_and_sides_count_toward_inventory_demand() -> None:
    order = Order(
        "non-pizza-order",
        "Alex",
        datetime(2026, 8, 12, 16, 0),
        (
            Item("Cucumber Salad", 2, "salad"),
            Item("Side Hot Honey", 3, "side"),
            Item("TCHO Miso Chocolate Chip Cookie", 2, "cookie"),
            Item("Mari T-Shirt", 1, "merch"),
            Item("Mexican Coke", 2, "drink"),
        ),
    )

    assert order.pizza_units == 0
    assert order.salad_counts == {"Cucumber Salad": 2}
    assert order.side_counts == {"Side Hot Honey": 3}
    assert order.cookie_count == 2
    assert order.drink_summary == (("Mexican Coke", 2),)
    assert [item.category for item in order.production_items] == [
        "salad",
        "side",
        "cookie",
        "merch",
    ]


def test_scheduled_non_pizza_order_stays_on_board_and_slot_is_not_empty() -> None:
    service_date = date(2026, 8, 12)
    slot = datetime(2026, 8, 12, 16, 0)
    order = Order(
        "salad-merch-only",
        "Alex",
        slot,
        (
            Item("Cucumber Salad", 1, "salad"),
            Item("Mari T-Shirt", 1, "merch"),
            Item("Saratoga Water", 1, "drink"),
        ),
    )

    board = build_service_board(service_date, (order,), pickup_times=(slot,))

    assert board.windows[0].orders == (order,)
    assert board.windows[0].pizza_units == 0
    assert board.windows[0].is_empty is False
    assert board.total_pizzas == 0


def test_non_pizza_production_walk_ins_are_visible_but_cookie_only_stays_hidden() -> None:
    service_date = date(2026, 8, 12)
    event_at = datetime(2026, 8, 12, 13, 15)
    salad = Order(
        "salad-walk-in",
        "Salad ticket",
        event_at,
        (Item("Cucumber Salad", 1, "salad"),),
        is_walk_in=True,
    )
    merch = Order(
        "merch-walk-in",
        "Merch ticket",
        event_at,
        (Item("Mari T-Shirt", 1, "merch"),),
        is_walk_in=True,
    )
    cookie = Order(
        "cookie-walk-in",
        "Cookie ticket",
        event_at,
        (Item("TCHO Miso Chocolate Chip Cookie", 1, "cookie"),),
        is_walk_in=True,
    )

    board = build_service_board(service_date, (salad, merch, cookie))

    assert board.unscheduled_orders == (merch, salad)
    assert cookie not in board.unscheduled_orders
