from pizzeria_dashboard.domain import Item, Modifier, Order, build_service_board
from pizzeria_dashboard.service_state import (
    ServiceState,
    build_inventory_summary,
    carryover_state,
)
from datetime import date, datetime


def test_carryover_uses_previous_remaining_and_drops_removed_menu_items() -> None:
    previous_state = ServiceState(
        dough_balls_prepared=30,
        salad_prepared={"Cucumber Salad": 8},
        side_prepared={"Side Ranch": 5, "Side Hot Honey": 4},
        cookie_prepared=12,
    )
    previous_orders = (
        Order(
            "order-1",
            "A",
            datetime(2026, 8, 8, 18, 0),
            (
                Item(
                    "Plain Pie",
                    1,
                    "pizza",
                    modifiers=(
                        Modifier("Cucumber Salad", "salad", 6),
                        Modifier("Side Ranch", "side", 2),
                        Modifier("Side Hot Honey", "side", 1),
                    ),
                ),
                Item("TCHO Miso Chocolate Chip Cookie", 7, "cookie"),
            ),
        ),
    )

    carried = carryover_state(
        previous_state,
        previous_orders,
        salad_types=("Cucumber Salad",),
        side_types=("Side Hot Honey",),
    )

    assert carried.dough_balls_prepared == 24
    assert carried.salad_prepared == {"Cucumber Salad": 2}
    assert carried.side_prepared == {"Side Hot Honey": 3}
    assert "Side Ranch" not in carried.side_prepared
    assert carried.cookie_prepared == 5


def test_inventory_counts_cookie_only_walk_ins_even_when_hidden_from_board() -> None:
    service_date = date(2026, 8, 9)
    cookie_walk_in = Order(
        "cookie-only",
        "Walk-in",
        datetime(2026, 8, 9, 12, 0),
        (Item("TCHO Miso Chocolate Chip Cookie", 2, "cookie"),),
        is_walk_in=True,
    )
    service = build_service_board(
        service_date,
        (cookie_walk_in,),
        pickup_times=(datetime(2026, 8, 9, 12, 0),),
    )
    state = ServiceState(24, {}, {}, 5)

    inventory = build_inventory_summary(
        service,
        state,
        salad_types=(),
        side_types=(),
        orders=(cookie_walk_in,),
    )

    assert service.total_cookies == 0
    assert inventory.cookies[0].ordered == 2
    assert inventory.cookies[0].remaining == 3
