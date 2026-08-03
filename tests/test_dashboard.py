import sqlite3
from html import unescape
from html.parser import HTMLParser
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pizzeria_dashboard import create_app
from pizzeria_dashboard.database import (
    load_app_metadata,
    load_orders_for_date,
    load_service_state_payload,
)
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
    assert b"Pizzeria Mari Production Dashboard" not in response.data
    assert b'class="pizzeria-mari-logo"' in response.data
    assert b'pizzeria-mari-logo.png' in response.data
    assert b'data-service-setup-open' in response.data
    assert response.data.index(b'data-service-setup-open') < response.data.index(b'id="service-date"')
    assert b'id="service-setup-dialog"' in response.data
    assert b'Weekly pickup hours' in response.data
    assert "Tomato Pie" in visible_text
    assert "Pie breakdown" in visible_text
    assert "16 total" in visible_text
    assert b'class="pizza-summary-card"' not in response.data
    dough_card_start = response.data.index(b'class="operations-card operations-card--dough"')
    dough_card_end = response.data.index(b'class="operations-card operations-card--salads"')
    dough_card_html = response.data[dough_card_start:dough_card_end]
    assert "5× Plain Pie".encode() in dough_card_html
    assert "5× Plain Pie" in visible_text
    assert "4× Tomato Pie" in visible_text
    assert "4× Weekly Special" in visible_text
    assert "3× White Pie" in visible_text
    assert dough_card_html.index("5× Plain Pie".encode()) < dough_card_html.index("4× Tomato Pie".encode())
    assert dough_card_html.index("4× Tomato Pie".encode()) < dough_card_html.index("4× Weekly Special".encode())
    assert dough_card_html.index("4× Weekly Special".encode()) < dough_card_html.index("3× White Pie".encode())
    assert b"Receipt FCMu" not in response.data
    assert b"3 pizzas" in response.data
    assert response.data.count(b'class="pickup-window') == 16
    assert response.data.count(b'class="order-row') >= 3
    assert len(load_orders_for_date(Path(app.config["DATABASE_PATH"]), date(2026, 7, 31))) == 14


def test_prep_view_toggle_marks_empty_and_past_slots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            7,
            31,
            17,
            0,
            1,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-prep-view-control' in html
    assert 'data-prep-view-toggle' in html
    assert 'data-service-timezone="America/New_York"' in html
    assert 'data-prep-view-empty' in html

    past_slot_start = html.index('data-pickup-at="2026-07-31T16:00:00"')
    past_slot_end = html.index(">", past_slot_start)
    past_slot = html[past_slot_start:past_slot_end]
    assert 'data-slot-empty="false"' in past_slot
    assert 'data-slot-past="true"' in past_slot

    empty_slot_start = html.index('data-pickup-at="2026-07-31T17:30:00"')
    empty_slot_end = html.index(">", empty_slot_start)
    empty_slot = html[empty_slot_start:empty_slot_end]
    assert 'data-slot-empty="true"' in empty_slot
    assert 'data-slot-past="false"' in empty_slot


def test_today_square_dashboard_has_incremental_and_auto_refresh_controls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            7,
            31,
            12,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    app = _test_app(
        tmp_path,
        ORDER_SOURCE="square",
        SQUARE_ACCESS_TOKEN="token",
        SQUARE_LOCATION_ID="LOCATION-1",
        AUTO_SEED_SAMPLE_DATA=False,
    )
    response = app.test_client().get("/?date=2026-07-31")

    assert response.status_code == 200
    assert b"Incremental update" in response.data
    assert b"Auto refresh" in response.data
    assert b'data-auto-sync-toggle' in response.data
    assert b'data-auto-sync-interval' in response.data
    assert b'data-incremental-sync-available="true"' in response.data
    assert b'data-auto-sync-available="true"' in response.data
    assert b"Full refresh from Square" in response.data


def test_future_square_dashboard_allows_incremental_and_auto_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            7,
            31,
            12,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    app = _test_app(
        tmp_path,
        ORDER_SOURCE="square",
        SQUARE_ACCESS_TOKEN="token",
        SQUARE_LOCATION_ID="LOCATION-1",
        AUTO_SEED_SAMPLE_DATA=False,
    )
    response = app.test_client().get("/?date=2026-08-01")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-incremental-sync-available="true"' in html
    assert 'data-auto-sync-available="true"' in html
    button_start = html.index('data-incremental-sync-button')
    button_end = html.index('>', button_start)
    assert "disabled" not in html[button_start:button_end]
    toggle_start = html.index('data-auto-sync-toggle')
    toggle_end = html.index('>', toggle_start)
    assert "disabled" not in html[toggle_start:toggle_end]
    assert "Today &amp; future only" not in html



def test_historical_square_dashboard_still_shows_disabled_refresh_controls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            7,
            31,
            12,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    app = _test_app(
        tmp_path,
        ORDER_SOURCE="square",
        SQUARE_ACCESS_TOKEN="token",
        SQUARE_LOCATION_ID="LOCATION-1",
        AUTO_SEED_SAMPLE_DATA=False,
    )
    response = app.test_client().get("/?date=2026-07-24")

    assert response.status_code == 200
    assert b"Incremental update" in response.data
    assert b"Auto refresh" in response.data
    assert b"Today &amp; future only" in response.data
    assert b'data-incremental-sync-button' in response.data
    assert b'Incremental updates are available for today and future service dates.' in response.data
    assert b'data-incremental-sync-available="false"' in response.data
    assert b'data-auto-sync-toggle' in response.data


def test_auto_refresh_preferences_persist_in_sqlite(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    response = client.post(
        "/auto-refresh-settings",
        json={"enabled": False, "seconds": 120},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "enabled": False, "seconds": 120}
    database_path = Path(app.config["DATABASE_PATH"])
    assert load_app_metadata(database_path, "square_auto_refresh_enabled") == "false"
    assert load_app_metadata(database_path, "square_auto_refresh_seconds") == "120"


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
    assert b'data-capacity-drop-zone' in response.data
    assert b'data-open-pizza-spaces="2"' in response.data
    assert b'data-open-pizza-spaces="1"' in response.data
    assert b'data-pickup-at="2026-07-31T16:30:00"' in response.data
    assert b'data-pickup-at="2026-07-31T17:30:00"' in response.data
    assert b'Drop a walk-in order here to assign' in response.data


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
    from zoneinfo import ZoneInfo
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


def test_order_note_is_visible_on_main_order_card(tmp_path: Path) -> None:
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (
            Order(
                order_id="noted-order",
                customer_name="Alex",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(Item("Plain Pie", 1, "pizza"),),
                note="Please leave this pie uncut.\nCustomer will arrive early.",
            ),
        ),
        source="sample",
    )

    response = app.test_client().get("/?date=2026-07-31")
    visible_text = _visible_text(response)
    cached_order = load_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected
    )[0]

    assert response.status_code == 200
    assert "Order note" in visible_text
    assert "Please leave this pie uncut." in visible_text
    assert "Customer will arrive early." in visible_text
    assert cached_order.note == "Please leave this pie uncut.\nCustomer will arrive early."



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


def test_sample_order_details_are_privacy_preserving_without_debug_data(tmp_path: Path) -> None:
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
    assert b"Pickup time" in response.data
    assert b"Debug" not in response.data
    assert b"Mexican Coke" not in response.data
    assert b"Cached dashboard document" not in response.data
    assert client.get(
        "/order-debug",
        query_string={"date": "2026-07-31", "order_id": "sample-2026-07-31-PM-1042"},
    ).status_code == 404


def test_customer_names_are_compact_on_board_and_full_in_order_modal(tmp_path: Path) -> None:
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
                order_id="private-name",
                customer_name="Alex Christopher",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(Item("Plain Pie", 1, "pizza"),),
            ),
        ),
        source="sample",
    )

    client = app.test_client()
    dashboard = client.get("/?date=2026-07-31")
    assert b"Alex C." in dashboard.data
    assert b"Alex Christopher" not in dashboard.data

    details = client.get(
        "/order-details",
        query_string={"date": "2026-07-31", "order_id": "private-name"},
    )
    assert b"Alex Christopher" in details.data


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
    assert response.data.count(b"data-oven-position-key=") == pizza_line_items
    assert response.data.count(b'data-oven-position-choice="top-left"') == pizza_line_items
    assert response.data.count(b'data-oven-position-choice="top-right"') == pizza_line_items
    assert response.data.count(b'data-oven-position-choice="bottom-left"') == pizza_line_items
    assert response.data.count(b'data-oven-position-choice="bottom-right"') == pizza_line_items
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
    assert b"data-oven-position" in response.data
    assert b"data-bake-timer-toggle" in response.data


def test_past_dates_hide_bake_timers(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2000-01-01")

    assert response.status_code == 200
    assert b"data-bake-timer" not in response.data
    assert b"data-bake-timer-toggle" not in response.data
    assert b"data-bake-timer-reset" not in response.data
    assert b"data-oven-position" not in response.data


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
    assert b'data-walk-in-pizza-units="2"' in response.data
    assert b'data-walk-in-drop-zone' in response.data
    assert b'data-capacity-drop-zone' in response.data

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


def test_ticket_name_auto_assigns_walk_in_and_modal_can_override_slot(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from pizzeria_dashboard.database import (
        load_order_slot_assignment_overrides,
        replace_orders_for_date,
    )
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    database_path = Path(app.config["DATABASE_PATH"])
    walk_in = Order(
        order_id="walk-in-ticket-time",
        customer_name="Sam 7:30",
        pickup_at=datetime.fromisoformat("2026-07-31T13:07:00-04:00"),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="walk-in-ticket-time",
        is_walk_in=True,
        source_created_at=datetime.fromisoformat("2026-07-31T13:05:00-04:00"),
        source_closed_at=datetime.fromisoformat("2026-07-31T13:07:00-04:00"),
        ticket_name="Sam 7:30",
    )
    replace_orders_for_date(database_path, selected, (walk_in,), source="square")

    client = app.test_client()
    response = client.get("/?date=2026-07-31")
    html = response.get_data(as_text=True)
    slot_start = html.index('data-pickup-at="2026-07-31T19:30:00"')
    slot_end = html.find('class="pickup-window', slot_start + 1)
    slot_html = html[slot_start : slot_end if slot_end != -1 else None]
    assert 'data-walk-in-order-id="walk-in-ticket-time"' in slot_html
    assert 'data-order-details-trigger' in slot_html
    assert "0 waiting" in _visible_text(response)

    details = client.get(
        "/order-details",
        query_string={"date": "2026-07-31", "order_id": walk_in.order_id},
    )
    details_html = details.get_data(as_text=True)
    assert details.status_code == 200
    assert 'data-walk-in-assignment-form' in details_html
    assert 'value="2026-07-31T19:30:00"' in details_html
    assert "Automatically parsed from the Ticket Name" in details_html
    assert ">7:30 PM</option>" in details_html

    unassigned = client.post(
        "/walk-in-assignment",
        json={
            "service_date": "2026-07-31",
            "order_id": walk_in.order_id,
            "pickup_at": "",
        },
    )
    assert unassigned.status_code == 200
    assert load_order_slot_assignment_overrides(database_path, selected) == {
        walk_in.order_id: None
    }

    after = client.get("/?date=2026-07-31")
    after_text = _visible_text(after)
    assert "1 waiting" in after_text
    assert "Sam 7:30" in after_text


def test_open_square_order_can_be_marked_completed(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order
    import pizzeria_dashboard.dashboard as dashboard_module

    class FakeSquareClient:
        def __init__(self, settings):
            self.settings = settings

        def complete_order(self, order_id, *, fulfillment_uid):
            assert order_id == "square-order-1"
            assert fulfillment_uid == "pickup-1"
            return {
                "id": order_id,
                "state": "COMPLETED",
                "version": 8,
                "updated_at": "2026-07-31T20:05:00Z",
                "fulfillments": [
                    {
                        "uid": fulfillment_uid,
                        "type": "PICKUP",
                        "state": "COMPLETED",
                    }
                ],
            }

    monkeypatch.setattr(dashboard_module, "SquareClient", FakeSquareClient)
    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(2026, 7, 31, 16, 5, tzinfo=ZoneInfo("America/New_York")),
    )
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
                order_id="cached-order-1",
                customer_name="Alex R.",
                pickup_at=datetime(2026, 7, 31, 16, 0),
                items=(Item("Plain Pie", 1, "pizza"),),
                square_order_id="square-order-1",
                square_version=7,
                location_id="LOCATION-1",
                fulfillment_uid="pickup-1",
                fulfillment_state="RESERVED",
            ),
        ),
        source="square",
    )

    client = app.test_client()
    dashboard = client.get("/?date=2026-07-31")
    assert dashboard.status_code == 200
    assert b'data-order-complete-button' in dashboard.data
    assert b'>Release candidate</button>' in dashboard.data
    assert b'>Complete</button>' not in dashboard.data

    response = client.post(
        "/order-complete",
        json={"service_date": "2026-07-31", "order_id": "cached-order-1"},
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "already_completed": False,
        "order_state": "COMPLETED",
        "fulfillment_state": "COMPLETED",
    }

    cached = load_orders_for_date(Path(app.config["DATABASE_PATH"]), selected)
    assert len(cached) == 1
    assert cached[0].released is True
    assert cached[0].fulfillment_state == "COMPLETED"
    assert cached[0].square_version == 8

    refreshed = client.get("/?date=2026-07-31")
    assert b'data-order-complete-button' not in refreshed.data
    assert b"Capacity released" in refreshed.data


def test_customer_history_tags_and_lazy_modal_history(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pizzeria_dashboard.customer_history import CustomerHistoryOrder
    from pizzeria_dashboard.database import replace_customer_history
    from pizzeria_dashboard.domain import Item, Modifier

    app = _test_app(tmp_path)
    client = app.test_client()
    selected_date = date(2026, 7, 31)
    # Seed the normal sample order cache first.
    assert client.get("/?date=2026-07-31").status_code == 200

    current_order_id = "sample-2026-07-31-PM-1042"
    history_orders = []
    for number in range(1, 6):
        order_id = current_order_id if number == 5 else f"historical-{number}"
        history_orders.append(
            CustomerHistoryOrder(
                customer_id="customer-alex",
                order_id=order_id,
                ordered_at=datetime(2026, 7, number, 20, 0, tzinfo=UTC),
                service_date=date(2026, 7, number),
                source="Square Online",
                items=(
                    Item(
                        "Collar City" if number == 4 else "Plain Pie",
                        1,
                        "pizza",
                        modifiers=(Modifier("Spring Beet Salad", "salad"),),
                    ),
                ),
            )
        )
    replace_customer_history(
        Path(app.config["DATABASE_PATH"]),
        history_orders,
        synced_at=datetime(2026, 8, 2, tzinfo=UTC),
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        payment_count=5,
    )

    dashboard = client.get("/?date=2026-07-31")
    assert dashboard.status_code == 200
    assert "Regular · 5 orders".encode() in dashboard.data
    assert b"Customer visits" in dashboard.data
    assert b"14 online orders" in dashboard.data
    assert b"1 with history" in dashboard.data
    assert b"First timers" in dashboard.data
    assert "2nd–4th orders".encode() in dashboard.data
    assert b"History unavailable" in dashboard.data
    assert b"Build customer history" not in dashboard.data  # sample mode

    details = client.get(
        "/order-details?date=2026-07-31&order_id=sample-2026-07-31-PM-1042"
    )
    assert details.status_code == 200
    assert b"Customer history" in details.data
    assert b"data-customer-history-url" in details.data
    assert b"Collar City" not in details.data  # history remains lazy-loaded

    history = client.get(
        "/customer-history?date=2026-07-31&order_id=sample-2026-07-31-PM-1042"
    )
    assert history.status_code == 200
    assert "Regular · 5 orders".encode() in history.data
    assert b"Collar City" in history.data
    assert b"Spring Beet Salad" in history.data
    assert b"Current order" in history.data
    assert b"customer-alex" not in history.data


def test_walk_in_orders_do_not_show_customer_history_tags(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pizzeria_dashboard.customer_history import CustomerHistoryOrder
    from pizzeria_dashboard.database import replace_customer_history, replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    database_path = Path(app.config["DATABASE_PATH"])
    walk_in = Order(
        order_id="walk-in-with-history",
        customer_name="Sam 7:45",
        pickup_at=datetime.fromisoformat("2026-07-31T19:45:00-04:00"),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="walk-in-with-history",
        is_walk_in=True,
        source_created_at=datetime.fromisoformat("2026-07-31T19:35:00-04:00"),
        source_closed_at=datetime.fromisoformat("2026-07-31T19:37:00-04:00"),
        ticket_name="Sam 7:45",
    )
    replace_orders_for_date(database_path, selected, (walk_in,), source="square")
    replace_customer_history(
        database_path,
        tuple(
            CustomerHistoryOrder(
                customer_id="customer-sam",
                order_id=(
                    walk_in.square_order_id
                    if number == 5
                    else f"historical-walk-in-{number}"
                ),
                ordered_at=datetime(2026, 7, number, 20, 0, tzinfo=UTC),
                service_date=date(2026, 7, number),
                source="Square POS",
                items=(Item("Plain Pie", 1, "pizza"),),
            )
            for number in range(1, 6)
        ),
        synced_at=datetime(2026, 8, 2, tzinfo=UTC),
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        payment_count=5,
    )

    response = app.test_client().get("/?date=2026-07-31")

    assert response.status_code == 200
    assert b"Sam 7:45" in response.data
    assert b"Regular" not in response.data
    assert b"First Timer" not in response.data
    assert b"badge--customer" not in response.data
