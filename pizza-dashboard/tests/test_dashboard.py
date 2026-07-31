import sqlite3
from html import unescape
from html.parser import HTMLParser
from datetime import date
from pathlib import Path

from pizzeria_dashboard import create_app
from pizzeria_dashboard.database import load_orders_for_date, load_service_state_payload
from pizzeria_dashboard.sample_data import build_sample_service
from pizzeria_dashboard.service_config import load_configuration
from pizzeria_dashboard.service_state import build_inventory_summary, default_state


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_text(response) -> str:
    parser = _VisibleTextParser()
    parser.feed(response.get_data(as_text=True))
    return " ".join(parser.parts)


def _test_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_PATH": str(tmp_path / "dashboard.db"),
        "LEGACY_SERVICE_STATE_PATH": str(tmp_path / "service_state.json"),
        "AUTO_SEED_SAMPLE_DATA": True,
        "ORDER_SOURCE": "sample",
    }
    config.update(overrides)
    return create_app(config)


def test_dashboard_renders_cached_orders_and_pizza_totals(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    response = app.test_client().get("/?date=2026-07-31")

    assert response.status_code == 200
    visible_text = _visible_text(response)
    assert "Production board" not in visible_text
    assert "Friday, July 31, 2026" not in visible_text
    assert b"Sample + SQLite" in response.data
    assert b'class="masthead-tools masthead-tools--single-row"' in response.data
    assert b"Tomato Pie" in response.data
    assert b"Receipt FCMu" not in response.data
    assert b"3 pizzas" in response.data
    assert response.data.count(b'class="pickup-window') == 16
    assert response.data.count(b'class="order-row') >= 3
    assert len(load_orders_for_date(Path(app.config["DATABASE_PATH"]), date(2026, 7, 31))) == 14


def test_release_candidates_consider_total_slot_capacity() -> None:
    service = build_sample_service()
    candidates = {
        order.order_id.rsplit("-", 1)[-1]
        for window in service.windows
        for order in window.orders
        if service.is_release_candidate(order, window)
    }

    assert candidates == {"1047", "1053"}
    assert service.release_candidates == 2
    assert [window.pickup_at.strftime("%-I:%M %p") for window in service.release_candidate_windows] == [
        "4:30 PM",
        "5:00 PM",
    ]


def test_open_capacity_card_lists_one_and_two_pie_slots(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    assert b"Open pizza capacity" in response.data
    assert b'operations-card--sides' not in response.data
    assert b"Salads, sides &amp; cookies" in response.data
    assert b"Cucumber Salad" in response.data
    assert b"Side Ranch" in response.data
    assert b"Cookies" in response.data
    assert b"2-pie orders" in response.data
    assert b"1-pie orders" in response.data
    assert b'aria-label="Slots available for one-pie orders"' in response.data
    assert b'datetime="2026-07-31T16:30:00"' in response.data
    assert b'datetime="2026-07-31T17:30:00"' in response.data


def test_modifiers_and_salads_render_from_cached_documents(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    assert b"Plain Pie" in response.data
    assert b"Pepperoni" in response.data
    assert b"Pickled chiles" in response.data
    assert b"Basil" in response.data
    assert "1× Cucumber Salad".encode() in response.data
    assert "1× Kale Caesar Salad".encode() in response.data
    assert "2× Cookie".encode() in response.data
    assert b"modifier--cookie" not in response.data
    assert b"modifier--salad" not in response.data
    assert b"modifier--side" not in response.data
    assert "1× Side Hot Honey".encode() in response.data
    assert "1× Side Ranch".encode() in response.data
    assert b"modifier--removal" in response.data
    assert b"No garlic" in response.data
    rendered = unescape(response.get_data(as_text=True))
    assert "Don't cut" in rendered
    assert "Double Cut" in rendered



def test_production_view_hides_trailing_parenthetical_descriptions(tmp_path: Path) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Modifier, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (
            Order(
                order_id="descriptive-order",
                customer_name="Alex",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(
                    Item(
                        "Spring Pie (local beets, butterhead, etc.)",
                        1,
                        "pizza",
                        modifiers=(
                            Modifier(
                                "Spring Beet Salad (local beets, butterhead, etc.)",
                                "salad",
                            ),
                        ),
                    ),
                ),
            ),
        ),
        source="sample",
    )

    response = app.test_client().get("/?date=2026-07-31")
    visible_text = _visible_text(response)

    assert "Spring Pie" in visible_text
    assert "1× Spring Beet Salad" in visible_text
    assert "local beets, butterhead, etc." not in visible_text



def test_past_dates_hide_capacity_released_badges(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2000-01-01")

    assert response.status_code == 200
    assert b"Capacity released" not in response.data


def test_guest_orders_do_not_show_receipt_or_square_reference_tags(tmp_path: Path) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (
            Order(
                order_id="cache-order",
                customer_name="Guest",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(Item("Plain Pie", 1, "pizza"),),
                receipt_number="FCMu",
                square_order_id="abcdefghijklmnop",
            ),
        ),
        source="square",
    )

    response = app.test_client().get("/?date=2026-07-31")
    visible_text = _visible_text(response)
    assert "Guest" in visible_text
    assert "Receipt FCMu" not in visible_text
    assert "Square ijklmnop" not in visible_text


def test_order_cards_open_the_details_dialog(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")

    assert b'data-order-details-url=' in response.data
    assert b'id="order-details-dialog"' in response.data
    assert b'View details' in response.data
    assert b'dashboard.js' in response.data


def test_sample_order_details_include_hidden_items_and_cached_json(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    client.get("/?date=2026-07-31")

    response = client.get(
        "/order-details",
        query_string={
            "date": "2026-07-31",
            "order_id": "sample-2026-07-31-PM-1042",
        },
    )

    assert response.status_code == 200
    assert b"Alex R." in response.data
    assert b"Receipt FCMu" in response.data
    assert b"Mexican Coke" in response.data
    assert b"Cached dashboard document" in response.data
    assert b"sample data" in response.data


def test_guest_order_details_fetch_live_square_order_and_payment(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order
    import pizzeria_dashboard.dashboard as dashboard_module

    class FakeSquareClient:
        def __init__(self, settings):
            self.settings = settings

        def retrieve_order(self, order_id):
            assert order_id == "square-guest-order"
            return {
                "id": order_id,
                "state": "OPEN",
                "customer_id": "CUSTOMER-1",
                "created_at": "2026-07-30T18:00:00Z",
                "updated_at": "2026-07-31T18:30:00Z",
                "creation_source": {
                    "name": "Square Online",
                    "product": "SQUARE_ONLINE",
                },
                "fulfillments": [
                    {
                        "uid": "pickup-1",
                        "state": "PROPOSED",
                        "pickup_details": {
                            "pickup_at": "2026-07-31T20:00:00Z",
                            "recipient": {},
                        },
                    }
                ],
                "tenders": [{"payment_id": "payment-1"}],
            }

        def get_payment(self, payment_id):
            assert payment_id == "payment-1"
            return {
                "id": payment_id,
                "order_id": "square-guest-order",
                "receipt_number": "FCMu",
                "status": "COMPLETED",
                "card_details": {"card": {"fingerprint": "secret-card-fingerprint"}},
            }

    monkeypatch.setattr(dashboard_module, "SquareClient", FakeSquareClient)
    app = _test_app(
        tmp_path,
        AUTO_SEED_SAMPLE_DATA=False,
        ORDER_SOURCE="square",
        SQUARE_ACCESS_TOKEN="test-token",
    )
    selected = date(2026, 7, 31)
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (
            Order(
                order_id="cache-guest",
                customer_name="Guest",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(Item("Plain Pie", 1, "pizza"),),
                square_order_id="square-guest-order",
                fulfillment_uid="pickup-1",
                fulfillment_state="PROPOSED",
            ),
        ),
        source="square",
    )

    response = app.test_client().get(
        "/order-details",
        query_string={"date": "2026-07-31", "order_id": "cache-guest"},
    )

    assert response.status_code == 200
    assert b"Guest debugging clues" in response.data
    assert b"No display name" in response.data
    assert b"SQUARE_ONLINE" in response.data
    assert b"CUSTOMER-1" in response.data
    assert b"payment-1" in response.data
    assert b"FCMu" in response.data
    assert b"[redacted]" in response.data
    assert b"secret-card-fingerprint" not in response.data


def test_drinks_and_order_numbers_are_hidden(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    visible_text = _visible_text(response)

    assert "Mexican Coke" not in visible_text
    assert "Sparkling Water" not in visible_text
    assert "Jamie Q." not in visible_text
    assert "PM-1042" not in visible_text


def test_inventory_persists_in_sqlite_by_service_date(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/inventory",
        data={
            "service_date": "2026-07-31",
            "dough_balls_prepared": "30",
            "salad_cucumber_salad": "12",
            "salad_kale_caesar_salad": "9",
            "side_name": ["Side Hot Honey", "Side Ranch"],
            "side_side_hot_honey": "7",
            "side_side_ranch": "5",
            "cookie_prepared": "18",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    payload = load_service_state_payload(
        Path(app.config["DATABASE_PATH"]), date(2026, 7, 31)
    )
    assert payload == {
        "dough_balls_prepared": 30,
        "salad_prepared": {
            "Cucumber Salad": 12,
            "Kale Caesar Salad": 9,
        },
        "side_prepared": {
            "Side Hot Honey": 7,
            "Side Ranch": 5,
        },
        "cookie_prepared": 18,
    }
    assert b'value="30"' in response.data


def test_sync_replaces_cache_and_records_sync_time(tmp_path: Path) -> None:
    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    client = app.test_client()

    empty = client.get("/?date=2026-07-24")
    assert b"Open slot" in empty.data
    assert b"Tomato Pie" not in empty.data

    response = client.post(
        "/sync", data={"service_date": "2026-07-24"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"Loaded 14 sample orders" in response.data
    assert b"Tomato Pie" in response.data


def test_selected_dates_are_cached_independently(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    first = client.get("/?date=2026-07-24")
    second = client.get("/?date=2026-07-25")
    assert b'value="2026-07-24"' in first.data
    assert b'value="2026-07-25"' in second.data
    assert b"Friday, July 24, 2026" not in first.data
    assert b"Saturday, July 25, 2026" not in second.data

    with sqlite3.connect(app.config["DATABASE_PATH"]) as connection:
        dates = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT service_date FROM orders ORDER BY service_date"
            )
        }
    assert dates == {"2026-07-24", "2026-07-25"}


def test_inventory_summary_still_calculates_dough_remaining() -> None:
    service = build_sample_service()
    inventory = build_inventory_summary(service, default_state())
    assert inventory.dough_ordered == 16
    assert inventory.dough_remaining == 8
    assert inventory.cookies[0].prepared == 0
    assert inventory.cookies[0].ordered == 2
    assert inventory.cookies[0].remaining == -2


def test_service_setup_persists_hours_and_salad_lineup(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    response = client.post(
        "/settings",
        data={
            "service_date": "2026-07-31",
            "day_4_enabled": "on",
            "day_4_start": "17:00",
            "day_4_end": "18:00",
            "salad_types": "Tomato Salad\nLittle Gem Salad",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Service setup saved" in response.data

    configuration = load_configuration(Path(app.config["DATABASE_PATH"]))
    friday = configuration.days[4]
    assert friday.enabled is True
    assert friday.start_value == "17:00"
    assert friday.end_value == "18:00"
    assert len(configuration.pickup_times(date(2026, 7, 31))) == 4
    assert configuration.salad_types == ("Tomato Salad", "Little Gem Salad")
    assert configuration.side_types == ("Side Ranch", "Side Hot Honey")

    # Real cached orders outside newly shortened hours intentionally stay visible.
    assert b"Tomato Salad" in response.data
    assert b"Little Gem Salad" in response.data
    assert b'5:00 PM' in response.data


def test_closed_day_displays_configuration_empty_state(tmp_path: Path) -> None:
    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    response = app.test_client().get("/?date=2026-07-29")

    assert response.status_code == 200
    assert b"configured as closed" in response.data


def test_health_endpoint(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_each_pizza_line_item_has_an_eight_minute_bake_timer_for_today(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    app = _test_app(tmp_path)
    today = datetime.now(ZoneInfo(app.config["SERVICE_TIMEZONE"])).date()
    response = app.test_client().get(f"/?date={today.isoformat()}")

    service = build_sample_service(today)
    pizza_line_items = sum(
        1
        for window in service.windows
        for order in window.orders
        for item in order.production_items
        if item.category == "pizza"
    )

    assert response.status_code == 200
    assert response.data.count(b"data-bake-timer-key=") == pizza_line_items
    assert response.data.count(b'data-bake-duration-seconds="480"') == pizza_line_items
    assert b"data-bake-timer-toggle" in response.data
    assert b"data-bake-timer-reset" in response.data
    assert b">8:00<" in response.data


def test_future_dates_show_bake_timers(tmp_path: Path) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    app = _test_app(tmp_path)
    future_date = datetime.now(ZoneInfo(app.config["SERVICE_TIMEZONE"])).date() + timedelta(days=7)
    response = app.test_client().get(f"/?date={future_date.isoformat()}")

    assert response.status_code == 200
    assert b"data-bake-timer" in response.data
    assert b"data-bake-timer-toggle" in response.data


def test_past_dates_hide_bake_timers(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2000-01-01")

    assert response.status_code == 200
    assert b"data-bake-timer" not in response.data
    assert b"data-bake-timer-toggle" not in response.data
    assert b"data-bake-timer-reset" not in response.data


def test_walk_in_orders_render_unscheduled_and_can_be_dragged_into_a_slot(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from pizzeria_dashboard.database import (
        load_order_slot_assignments,
        replace_orders_for_date,
    )
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    database_path = Path(app.config["DATABASE_PATH"])
    walk_in = Order(
        order_id="walk-in-square-1",
        customer_name="Ticket 42",
        pickup_at=datetime.fromisoformat("2026-07-31T13:07:00-04:00"),
        items=(Item("Plain Pie", 2, "pizza"),),
        square_order_id="walk-in-square-1",
        is_walk_in=True,
        source_created_at=datetime.fromisoformat("2026-07-31T13:05:00-04:00"),
        source_closed_at=datetime.fromisoformat("2026-07-31T13:07:00-04:00"),
        creation_product="SQUARE_POS",
        ticket_name="Ticket 42",
    )
    replace_orders_for_date(
        database_path,
        selected,
        (walk_in,),
        source="square",
    )

    client = app.test_client()
    response = client.get("/?date=2026-07-31")
    visible_text = _visible_text(response)
    assert response.status_code == 200
    assert "Unscheduled" in visible_text
    assert "Ticket 42" in visible_text
    assert "Paid 1:07 PM" in visible_text
    assert b'data-walk-in-order-id="walk-in-square-1"' in response.data
    assert b'data-walk-in-drop-zone' in response.data

    assigned = client.post(
        "/walk-in-assignment",
        json={
            "service_date": "2026-07-31",
            "order_id": "walk-in-square-1",
            "pickup_at": "2026-07-31T16:15:00",
        },
    )
    assert assigned.status_code == 200
    assert assigned.get_json() == {"ok": True}
    assert load_order_slot_assignments(database_path, selected) == {
        "walk-in-square-1": datetime(2026, 7, 31, 16, 15)
    }

    moved = client.get("/?date=2026-07-31")
    moved_text = _visible_text(moved)
    assert "0 waiting" in moved_text
    assert "All walk-ins are assigned" in moved_text
    assert "Ticket 42" in moved_text
    assert "4:15" in moved_text

    unassigned = client.post(
        "/walk-in-assignment",
        json={
            "service_date": "2026-07-31",
            "order_id": "walk-in-square-1",
            "pickup_at": "",
        },
    )
    assert unassigned.status_code == 200
    assert load_order_slot_assignments(database_path, selected) == {}


def test_walk_in_assignment_rejects_non_service_slot(tmp_path: Path) -> None:
    from datetime import datetime

    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (
            Order(
                "walk-in-square-2",
                "Walk-in",
                datetime(2026, 7, 31, 13, 15),
                (Item("Plain Pie", 1, "pizza"),),
                is_walk_in=True,
            ),
        ),
        source="square",
    )

    response = app.test_client().post(
        "/walk-in-assignment",
        json={
            "service_date": "2026-07-31",
            "order_id": "walk-in-square-2",
            "pickup_at": "2026-07-31T16:07:00",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Choose one of the configured service slots."
