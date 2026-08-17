import base64
import re
import secrets
from dataclasses import replace
import sqlite3
from html import unescape
from html.parser import HTMLParser
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pizzeria_dashboard import create_app
from pizzeria_dashboard.database import (
    load_app_metadata,
    load_order_internal_note,
    load_order_ready_states,
    load_orders_for_date,
    load_pie_production_states,
    load_service_state_payload,
    replace_orders_for_date,
)
from pizzeria_dashboard.sample_data import build_sample_orders, build_sample_service
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
    assert "Pies all day" in visible_text
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
    assert b'data-order-pizza-units=' in response.data
    assert b'data-total-pizzas="16"' in response.data
    assert len(load_orders_for_date(Path(app.config["DATABASE_PATH"]), date(2026, 7, 31))) == 14


def test_order_details_show_square_debug_identifiers(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    selected = date(2026, 7, 31)
    assert client.get(f"/?date={selected.isoformat()}").status_code == 200

    database_path = Path(app.config["DATABASE_PATH"])
    orders = load_orders_for_date(database_path, selected)
    target = orders[0]
    updated = replace(
        target,
        square_order_id="square-debug-order",
        reference_id="reference-debug-123",
        payment_ids=("payment-debug-1", "payment-debug-2"),
    )
    replace_orders_for_date(
        database_path,
        selected,
        (updated, *orders[1:]),
        source="sample",
    )

    response = client.get(
        f"/order-details?date={selected.isoformat()}&order_id={target.order_id}"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Order ID" in html
    assert "square-debug-order" in html
    assert "Reference ID" in html
    assert "reference-debug-123" in html
    assert "Payment IDs" in html
    assert "payment-debug-1" in html
    assert "payment-debug-2" in html


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


def test_incremental_refresh_detects_staff_note_changes_from_another_device(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    selected = date(2026, 8, 6)
    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026, 8, 6, 17, 0, tzinfo=ZoneInfo("America/New_York")
        ),
    )
    monkeypatch.setattr(
        dashboard_module,
        "_refresh_customer_history_after_order_sync",
        lambda **_kwargs: None,
    )
    app = _test_app(tmp_path)
    client = app.test_client()
    assert client.get(f"/?date={selected.isoformat()}").status_code == 200

    saved = client.post(
        "/order-note",
        json={
            "service_date": selected.isoformat(),
            "order_id": "sample-2026-08-06-PM-1042",
            "note": "Allergy note from the prep display",
        },
    )
    assert saved.status_code == 200

    refreshed = client.post(
        "/sync/quick",
        json={
            "service_date": selected.isoformat(),
            "board_content_revision": "",
        },
    )
    payload = refreshed.get_json()
    assert refreshed.status_code == 200
    assert payload["ok"] is True
    assert payload["board_content_changed"] is True
    assert payload["board_content_revision"] == saved.get_json()["board_content_revision"]


def test_service_notes_button_log_and_new_note_indicator(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    selected = date(2026, 8, 14)

    initial = client.get(f"/?date={selected.isoformat()}")
    assert initial.status_code == 200
    assert b"Service notes" in initial.data
    assert b'data-service-notes-open' in initial.data
    assert b'data-service-notes-latest-id="0"' in initial.data
    assert b"No notes yet for this service." in initial.data

    added = client.post(
        "/service-note",
        json={
            "service_date": selected.isoformat(),
            "note": "Out of sausage for additions",
        },
    )
    assert added.status_code == 200
    payload = added.get_json()
    assert payload["ok"] is True
    assert payload["note"]["text"] == "Out of sausage for additions"
    assert payload["note"]["id"] > 0
    assert payload["board_content_revision"]

    rendered = client.get(f"/?date={selected.isoformat()}")
    assert b"Out of sausage for additions" in rendered.data
    assert f'data-service-notes-latest-id="{payload["note"]["id"]}"'.encode() in rendered.data
    assert b'data-service-note-id=' in rendered.data

    another_day = client.get("/?date=2026-08-15")
    assert b"Out of sausage for additions" not in another_day.data

    javascript = Path("pizzeria_dashboard/static/dashboard.js").read_text()
    assert "pizzeria-dashboard:service-notes-seen:" in javascript
    assert "data-service-notes-unread" in rendered.get_data(as_text=True)
    assert "renderUnreadIndicator" in javascript
    assert "board.dataset.boardContentRevision" in javascript


def test_service_note_rejects_blank_and_oversized_text(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    selected = "2026-08-14"

    blank = client.post(
        "/service-note",
        json={"service_date": selected, "note": "   "},
    )
    assert blank.status_code == 400
    assert blank.get_json()["ok"] is False

    oversized = client.post(
        "/service-note",
        json={"service_date": selected, "note": "x" * 2001},
    )
    assert oversized.status_code == 400
    assert oversized.get_json()["ok"] is False


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
    assert b"Available pickup slots" in response.data
    assert b'operations-card--sides' not in response.data
    assert b"Salads, sides &amp; cookies" in response.data
    assert b"Cucumber Salad" in response.data
    assert b"Side Ranch" in response.data
    assert b"Cookies" in response.data
    assert b"Slots available for up to 2 pies" in response.data
    assert b"Slots available for only 1 pie" in response.data
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
    assert b"Modifiers all day" in response.data
    assert b"portions" in response.data
    assert b'class="modifier-prep-list"' in response.data
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



def test_staff_note_can_be_saved_from_order_details_and_appears_on_card(
    tmp_path: Path,
) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    selected = date(2026, 7, 31)
    order_id = "sample-2026-07-31-PM-1042"

    details = client.get(
        f"/order-details?date={selected.isoformat()}&order_id={order_id}"
    )
    details_html = details.get_data(as_text=True)
    assert details.status_code == 200
    assert 'data-order-note-form' in details_html
    assert 'data-order-note-clear' in details_html
    assert "Saved only in this dashboard" in details_html

    response = client.post(
        "/order-note",
        json={
            "service_date": selected.isoformat(),
            "order_id": order_id,
            "note": "  Allergy: change gloves.\nSubstitute aged mozzarella.  ",
        },
    )
    assert response.status_code == 200
    saved_payload = response.get_json()
    assert saved_payload["ok"] is True
    assert saved_payload["note"] == "Allergy: change gloves.\nSubstitute aged mozzarella."
    assert saved_payload["board_content_revision"]
    assert load_order_internal_note(
        Path(app.config["DATABASE_PATH"]), selected, order_id
    ) == "Allergy: change gloves.\nSubstitute aged mozzarella."

    board = client.get(f"/?date={selected.isoformat()}")
    visible_text = _visible_text(board)
    assert "Staff note" in visible_text
    assert "Allergy: change gloves." in visible_text
    assert "Substitute aged mozzarella." in visible_text
    assert b'order-note order-note--internal' in board.data

    details = client.get(
        f"/order-details?date={selected.isoformat()}&order_id={order_id}"
    )
    assert b"Allergy: change gloves." in details.data
    assert b"Substitute aged mozzarella." in details.data

    cleared = client.post(
        "/order-note",
        json={
            "service_date": selected.isoformat(),
            "order_id": order_id,
            "note": "",
        },
    )
    assert cleared.status_code == 200
    cleared_payload = cleared.get_json()
    assert cleared_payload["ok"] is True
    assert cleared_payload["note"] is None
    assert cleared_payload["board_content_revision"]
    assert load_order_internal_note(
        Path(app.config["DATABASE_PATH"]), selected, order_id
    ) is None

    board = client.get(f"/?date={selected.isoformat()}")
    assert b'order-note order-note--internal' not in board.data


def test_staff_note_rejects_missing_orders_and_oversized_text(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    missing = client.post(
        "/order-note",
        json={
            "service_date": "2026-07-31",
            "order_id": "missing",
            "note": "Test",
        },
    )
    assert missing.status_code == 404

    too_long = client.post(
        "/order-note",
        json={
            "service_date": "2026-07-31",
            "order_id": "sample-2026-07-31-PM-1042",
            "note": "x" * 2001,
        },
    )
    assert too_long.status_code == 400
    assert "2,000" in too_long.get_json()["error"]


def test_past_dates_hide_capacity_released_badges(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2000-01-01")

    assert response.status_code == 200
    assert b"Capacity Released" not in response.data


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
    assert b"Mexican Coke" in response.data
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


def test_drinks_render_as_compact_count_badges_while_order_numbers_stay_hidden(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    visible_text = _visible_text(response)

    assert "Drinks" in visible_text
    assert b"badge--drink" in response.data
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
            "slice_pies": "4",
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
        "slice_pies": 4,
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
    assert b'name="slice_pies"' in response.data
    assert b'value="4"' in response.data


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


def test_inventory_summary_subtracts_slice_pies_and_open_slot_reserve() -> None:
    service = build_sample_service()
    state = replace(default_state(), dough_balls_prepared=30, slice_pies=4)
    inventory = build_inventory_summary(
        service, state, open_slot_dough_reserve=6
    )

    assert inventory.dough_ordered == 16
    assert inventory.dough_slice_pies == 4
    assert inventory.dough_open_slot_reserve == 6
    assert inventory.dough_remaining == 4


def test_available_pickup_slots_use_twenty_minute_prep_buffer_and_reserve_dough(
    tmp_path: Path, monkeypatch
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    now = datetime(
        2026, 7, 31, 16, 6, tzinfo=ZoneInfo("America/New_York")
    )
    monkeypatch.setattr(dashboard_module, "_now", lambda: now)
    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    client = app.test_client()

    response = client.get("/?date=2026-07-31")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    availability_start = html.index("Available pickup slots")
    availability_end = html.index("Customer visits") if "Customer visits" in html else html.index("Service slots")
    availability = html[availability_start:availability_end]

    assert "20-minute preparation buffer" in availability
    assert 'datetime="2026-07-31T16:15:00"' not in availability
    assert 'datetime="2026-07-31T16:30:00"' in availability
    # 14 orderable slots remain from 4:30 through 7:45, each advertised for
    # up to two pies, so 28 dough balls are reserved for possible orders.
    assert "<strong>28</strong> open-slot reserve" in html
    dough_start = html.index("Dough inventory")
    dough_end = html.index("Available pickup slots", dough_start)
    dough_card = html[dough_start:dough_end]
    assert re.search(r">\s*-4\s*</strong>", dough_card)


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


def test_basic_auth_protects_dashboard_when_configured(tmp_path: Path) -> None:
    generated_user = secrets.token_urlsafe(12)
    generated_secret = secrets.token_urlsafe(24)
    app = _test_app(
        tmp_path,
        DASHBOARD_AUTH_USERNAME=generated_user,
        DASHBOARD_AUTH_PASSWORD=generated_secret,
    )
    client = app.test_client()

    unauthorized = client.get("/?date=2026-07-31")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"].startswith("Basic ")
    assert client.get("/healthz").status_code == 200

    encoded_credentials = base64.b64encode(
        f"{generated_user}:{generated_secret}".encode("utf-8")
    ).decode("ascii")
    authorized = client.get(
        "/?date=2026-07-31",
        headers={"Authorization": f"Basic {encoded_credentials}"},
    )
    assert authorized.status_code == 200
    assert authorized.headers["X-Content-Type-Options"] == "nosniff"
    assert authorized.headers["X-Frame-Options"] == "DENY"
    assert authorized.headers["Cache-Control"] == "no-store"


def test_each_pizza_line_item_has_an_eight_minute_bake_timer_for_today(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    app = _test_app(tmp_path)
    today = datetime.now(ZoneInfo(app.config["SERVICE_TIMEZONE"])).date()
    response = app.test_client().get(f"/?date={today.isoformat()}")

    service = build_sample_service(today)
    physical_pizzas = sum(
        item.quantity
        for window in service.windows
        for order in window.orders
        for item in order.production_items
        if item.category == "pizza"
    )

    assert response.status_code == 200
    assert response.data.count(b"data-bake-timer-key=") == physical_pizzas
    assert response.data.count(b"data-oven-position-key=") == physical_pizzas
    assert response.data.count(b'data-oven-position-choice="top-left"') == physical_pizzas
    assert response.data.count(b'data-oven-position-choice="top-right"') == physical_pizzas
    assert response.data.count(b'data-oven-position-choice="bottom-left"') == physical_pizzas
    assert response.data.count(b'data-oven-position-choice="bottom-right"') == physical_pizzas
    assert response.data.count(b'data-bake-duration-seconds="480"') == physical_pizzas
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


def test_shared_timer_oven_and_boxed_state_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    selected = date(2026, 8, 6)
    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026, 8, 6, 17, 0, tzinfo=ZoneInfo("America/New_York")
        ),
    )
    app = _test_app(tmp_path)
    client = app.test_client()
    board = client.get(f"/?date={selected.isoformat()}")
    html = board.get_data(as_text=True)
    pie_keys = re.findall(r'data-bake-timer-key="([^"]+)"', html)

    assert board.status_code == 200
    assert len(pie_keys) >= 2
    assert 'data-live-production-state-url="/live-production-state"' in html
    assert 'data-pie-production-state-url="/pie-production-state"' in html
    assert 'data-order-ready-url="/order-ready"' in html
    assert 'data-decrement-all-day-counts="true"' in html
    assert 'data-order-pizza-counts=' in html
    assert 'data-order-modifier-counts=' in html

    first = client.post(
        "/pie-production-state",
        json={
            "service_date": selected.isoformat(),
            "pie_key": pie_keys[0],
            "timer_action": "start",
            "duration_ms": 480_000,
        },
    )
    second = client.post(
        "/pie-production-state",
        json={
            "service_date": selected.isoformat(),
            "pie_key": pie_keys[1],
            "timer_action": "start",
            "duration_ms": 480_000,
        },
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["pies"][pie_keys[0]]["timer_status"] == "running"
    assert first.get_json()["pies"][pie_keys[0]]["oven_position"] == "top-left"
    assert second.get_json()["pies"][pie_keys[1]]["oven_position"] == "top-right"

    live = client.get(
        "/live-production-state", query_string={"date": selected.isoformat()}
    )
    live_payload = live.get_json()
    assert live.status_code == 200
    assert live_payload["pies"][pie_keys[0]]["oven_position"] == "top-left"
    assert live_payload["pies"][pie_keys[1]]["oven_position"] == "top-right"
    assert load_pie_production_states(
        Path(app.config["DATABASE_PATH"]), selected
    )[pie_keys[0]].timer_status == "running"

    order_id = "sample-2026-08-06-PM-1042"
    boxed = client.post(
        "/order-ready",
        json={
            "service_date": selected.isoformat(),
            "order_id": order_id,
            "boxed": True,
        },
    )
    assert boxed.status_code == 200
    assert boxed.get_json()["boxed_at"]
    assert order_id in load_order_ready_states(
        Path(app.config["DATABASE_PATH"]), selected
    )

    refreshed = client.get(f"/?date={selected.isoformat()}").get_data(as_text=True)
    row_start = refreshed.index(f'data-order-id="{order_id}"')
    row_tag_start = refreshed.rfind("<div", 0, row_start)
    row_tag_end = refreshed.index(">", row_start)
    row_end = refreshed.find('class="order-row', row_start + 1)
    order_html = refreshed[row_start : row_end if row_end != -1 else None]
    assert "order-row--boxed" in refreshed[row_tag_start:row_tag_end]
    assert "BOXED &amp; READY" in order_html
    assert "Undo boxed" in order_html


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
    assigned_payload = assigned.get_json()
    assert assigned_payload["ok"] is True
    assert assigned_payload["board_content_revision"]
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


def test_non_pizza_walk_in_does_not_render_on_production_board(tmp_path: Path) -> None:
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    event_at = datetime.fromisoformat("2026-07-31T13:07:00-04:00")
    cookie_only = Order(
        order_id="walk-in-cookie-only",
        customer_name="Cookie ticket",
        pickup_at=event_at,
        items=(Item("TCHO Miso Chocolate Chip Cookie", 1, "cookie"),),
        square_order_id="walk-in-cookie-only",
        is_walk_in=True,
        source_closed_at=event_at,
        creation_product="SQUARE_POS",
        ticket_name="Cookie ticket",
    )
    pizza_walk_in = Order(
        order_id="walk-in-pizza",
        customer_name="Pizza ticket",
        pickup_at=event_at,
        items=(
            Item("Plain Pie", 1, "pizza"),
            Item("TCHO Miso Chocolate Chip Cookie", 1, "cookie"),
        ),
        square_order_id="walk-in-pizza",
        is_walk_in=True,
        source_closed_at=event_at,
        creation_product="SQUARE_POS",
        ticket_name="Pizza ticket",
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]),
        selected,
        (cookie_only, pizza_walk_in),
        source="square",
    )

    response = app.test_client().get(f"/?date={selected.isoformat()}")
    visible_text = _visible_text(response)

    assert response.status_code == 200
    assert "Cookie ticket" not in visible_text
    assert "Pizza ticket" in visible_text
    assert b'data-walk-in-order-id="walk-in-cookie-only"' not in response.data
    assert b'data-walk-in-order-id="walk-in-pizza"' in response.data


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


def test_scheduled_order_pickup_time_can_be_adjusted_and_restored(
    tmp_path: Path,
) -> None:
    from pizzeria_dashboard.database import (
        load_order_slot_assignment_overrides,
        replace_orders_for_date,
    )
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    database_path = Path(app.config["DATABASE_PATH"])
    order = Order(
        order_id="scheduled-move-1",
        customer_name="Dana Move",
        pickup_at=datetime(2026, 7, 31, 16, 0),
        items=(Item("Plain Pie", 2, "pizza"),),
        square_order_id="scheduled-move-1",
        fulfillment_uid="pickup-1",
    )
    replace_orders_for_date(database_path, selected, (order,), source="square")
    client = app.test_client()

    details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": order.order_id},
    )
    details_html = details.get_data(as_text=True)
    assert details.status_code == 200
    assert "Adjust pickup time" in details_html
    assert "data-scheduled-pickup-time-form" in details_html
    assert "Original Square time — 4:00 PM" in details_html
    assert "4:15 PM — 0/3 pizzas" in details_html

    adjusted = client.post(
        "/scheduled-pickup-time",
        json={
            "service_date": selected.isoformat(),
            "order_id": order.order_id,
            "pickup_at": "2026-07-31T16:15:00",
        },
    )
    assert adjusted.status_code == 200
    adjusted_payload = adjusted.get_json()
    assert adjusted_payload["ok"] is True
    assert adjusted_payload["overridden"] is True
    assert adjusted_payload["pickup_at"] == "2026-07-31T16:15:00"
    assert adjusted_payload["board_content_revision"]
    assert load_order_slot_assignment_overrides(database_path, selected) == {
        order.order_id: datetime(2026, 7, 31, 16, 15)
    }

    moved = client.get(f"/?date={selected.isoformat()}")
    moved_html = moved.get_data(as_text=True)
    original_slot_start = moved_html.index('data-pickup-at="2026-07-31T16:00:00"')
    original_slot_end = moved_html.index('data-pickup-at="2026-07-31T16:15:00"')
    original_slot_html = moved_html[original_slot_start:original_slot_end]
    adjusted_slot_end = moved_html.find(
        'class="pickup-window', original_slot_end + 1
    )
    adjusted_slot_html = moved_html[
        original_slot_end : adjusted_slot_end if adjusted_slot_end != -1 else None
    ]
    assert "Dana M." not in original_slot_html
    assert "Dana M." in adjusted_slot_html
    assert "2 pizzas" in adjusted_slot_html
    assert "Moved from 4:00" in adjusted_slot_html

    # Source refreshes keep the local production override intact.
    replace_orders_for_date(database_path, selected, (order,), source="square")
    assert load_order_slot_assignment_overrides(database_path, selected) == {
        order.order_id: datetime(2026, 7, 31, 16, 15)
    }

    restored = client.post(
        "/scheduled-pickup-time",
        json={
            "service_date": selected.isoformat(),
            "order_id": order.order_id,
            "pickup_at": "original",
        },
    )
    assert restored.status_code == 200
    restored_payload = restored.get_json()
    assert restored_payload["ok"] is True
    assert restored_payload["overridden"] is False
    assert restored_payload["pickup_at"] == "2026-07-31T16:00:00"
    assert restored_payload["board_content_revision"]
    assert load_order_slot_assignment_overrides(database_path, selected) == {}


def test_current_day_pickup_editors_hide_elapsed_slots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order
    import pizzeria_dashboard.dashboard as dashboard_module

    selected = date(2026, 8, 6)
    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026, 8, 6, 17, 0, tzinfo=ZoneInfo("America/New_York")
        ),
    )
    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    database_path = Path(app.config["DATABASE_PATH"])
    scheduled = Order(
        "scheduled-future-slot",
        "Scheduled",
        datetime(2026, 8, 6, 17, 30),
        (Item("Plain Pie", 1, "pizza"),),
    )
    walk_in = Order(
        "walk-in-future-slot",
        "Walk-in",
        datetime(2026, 8, 6, 16, 30),
        (Item("Plain Pie", 1, "pizza"),),
        is_walk_in=True,
    )
    replace_orders_for_date(
        database_path, selected, (scheduled, walk_in), source="square"
    )
    client = app.test_client()

    scheduled_details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": scheduled.order_id},
    ).get_data(as_text=True)
    assert 'value="2026-08-06T16:45:00"' not in scheduled_details
    assert 'value="2026-08-06T17:00:00"' in scheduled_details
    assert 'value="2026-08-06T17:15:00"' in scheduled_details

    walk_in_details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": walk_in.order_id},
    ).get_data(as_text=True)
    assert 'value="2026-08-06T16:45:00"' not in walk_in_details
    assert 'value="2026-08-06T17:00:00"' in walk_in_details


def test_scheduled_pickup_time_rejects_walk_ins_and_non_service_slots(
    tmp_path: Path,
) -> None:
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    database_path = Path(app.config["DATABASE_PATH"])
    scheduled = Order(
        "scheduled-invalid-slot",
        "Scheduled",
        datetime(2026, 7, 31, 16, 0),
        (Item("Plain Pie", 1, "pizza"),),
    )
    walk_in = Order(
        "walk-in-invalid-editor",
        "Walk-in",
        datetime(2026, 7, 31, 13, 0),
        (Item("Plain Pie", 1, "pizza"),),
        is_walk_in=True,
    )
    replace_orders_for_date(
        database_path, selected, (scheduled, walk_in), source="square"
    )
    client = app.test_client()

    invalid_slot = client.post(
        "/scheduled-pickup-time",
        json={
            "service_date": selected.isoformat(),
            "order_id": scheduled.order_id,
            "pickup_at": "2026-07-31T16:07:00",
        },
    )
    assert invalid_slot.status_code == 400
    assert invalid_slot.get_json()["error"] == "Choose one of the configured service slots."

    wrong_editor = client.post(
        "/scheduled-pickup-time",
        json={
            "service_date": selected.isoformat(),
            "order_id": walk_in.order_id,
            "pickup_at": "2026-07-31T16:15:00",
        },
    )
    assert wrong_editor.status_code == 400
    assert "walk-in pickup editor" in wrong_editor.get_json()["error"]


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
    assert b'data-order-complete-button' not in dashboard.data
    assert b'>Release Capacity</button>' not in dashboard.data

    details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": "cached-order-1"},
    )
    assert details.status_code == 200
    assert b'data-order-complete-button' in details.data
    assert b'>Release Capacity</button>' in details.data
    assert b'>Complete</button>' not in details.data

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
    assert b"Capacity Released" not in refreshed.data

    refreshed_details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": "cached-order-1"},
    )
    assert refreshed_details.status_code == 200
    assert b"Capacity Released" in refreshed_details.data



def test_unpaid_open_square_order_can_be_removed_from_details(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order
    import pizzeria_dashboard.dashboard as dashboard_module

    class FakeSquareClient:
        def __init__(self, settings):
            self.settings = settings

        def cancel_unpaid_order(self, order_id):
            assert order_id == "square-abandoned-1"
            return {
                "id": order_id,
                "state": "CANCELED",
                "version": 3,
                "fulfillments": [
                    {
                        "uid": "pickup-1",
                        "type": "PICKUP",
                        "state": "CANCELED",
                    }
                ],
            }

    monkeypatch.setattr(dashboard_module, "SquareClient", FakeSquareClient)
    app = _test_app(
        tmp_path,
        AUTO_SEED_SAMPLE_DATA=False,
        ORDER_SOURCE="square",
        SQUARE_ACCESS_TOKEN="test-token",
    )
    selected = date(2026, 7, 31)
    order = Order(
        order_id="cached-abandoned-1",
        customer_name="Abandoned Guest",
        pickup_at=datetime(2026, 7, 31, 17, 0),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="square-abandoned-1",
        square_version=2,
        fulfillment_uid="pickup-1",
        fulfillment_state="PROPOSED",
        square_order_state="OPEN",
        is_paid=False,
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected, (order,), source="square"
    )
    client = app.test_client()

    details = client.get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": order.order_id},
    )
    assert details.status_code == 200
    assert b"Abandoned unpaid order" in details.data
    assert b"data-remove-unpaid-order" in details.data

    removed = client.post(
        "/order-remove-unpaid",
        json={"service_date": selected.isoformat(), "order_id": order.order_id},
    )
    assert removed.status_code == 200
    payload = removed.get_json()
    assert payload["ok"] is True
    assert payload["order_state"] == "CANCELED"
    assert payload["removed_count"] == 1
    assert load_orders_for_date(Path(app.config["DATABASE_PATH"]), selected) == ()


def test_paid_open_square_order_does_not_offer_unpaid_removal(tmp_path: Path) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    order = Order(
        order_id="cached-paid-open",
        customer_name="Paid Guest",
        pickup_at=datetime(2026, 7, 31, 17, 0),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="square-paid-open",
        fulfillment_uid="pickup-1",
        fulfillment_state="RESERVED",
        square_order_state="OPEN",
        is_paid=True,
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected, (order,), source="square"
    )

    details = app.test_client().get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": order.order_id},
    )
    assert details.status_code == 200
    assert b"data-remove-unpaid-order" not in details.data


def test_unpaid_order_is_prominently_flagged_on_main_card(tmp_path: Path) -> None:
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    unpaid = Order(
        order_id="cached-unpaid-main-card",
        customer_name="Unpaid Guest",
        pickup_at=datetime(2026, 7, 31, 17, 0),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="square-unpaid-main-card",
        square_order_state="OPEN",
        is_paid=False,
    )
    paid = Order(
        order_id="cached-paid-main-card",
        customer_name="Paid Guest",
        pickup_at=datetime(2026, 7, 31, 17, 15),
        items=(Item("Plain Pie", 1, "pizza"),),
        square_order_id="square-paid-main-card",
        square_order_state="OPEN",
        is_paid=True,
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected, (unpaid, paid), source="square"
    )

    response = app.test_client().get(f"/?date={selected.isoformat()}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "UNPAID — DO NOT PREP" in html
    assert 'data-order-id="cached-unpaid-main-card"' in html
    unpaid_start = html.index('data-order-id="cached-unpaid-main-card"')
    unpaid_class_start = html.rfind('class="order-row', 0, unpaid_start)
    unpaid_class_end = html.index('"', unpaid_class_start + len('class="'))
    assert "order-row--unpaid" in html[unpaid_class_start:unpaid_class_end]

    paid_start = html.index('data-order-id="cached-paid-main-card"')
    paid_class_start = html.rfind('class="order-row', 0, paid_start)
    paid_class_end = html.index('"', paid_class_start + len('class="'))
    assert "order-row--unpaid" not in html[paid_class_start:paid_class_end]


def test_today_dashboard_has_active_timer_rail_and_jump_to_top(tmp_path: Path, monkeypatch) -> None:
    app = _test_app(tmp_path)
    fixed_now = datetime(2026, 8, 13, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr("pizzeria_dashboard.dashboard._now", lambda: fixed_now)

    response = app.test_client().get("/?date=2026-08-13")

    assert response.status_code == 200
    assert b'data-active-timer-rail' in response.data
    assert b'data-jump-to-top' in response.data
    assert b'data-timer-order-id=' in response.data
    assert b'data-timer-pizza-name=' in response.data


def test_finish_timer_action_releases_oven_position_via_route(tmp_path: Path, monkeypatch) -> None:
    app = _test_app(tmp_path)
    fixed_now = datetime(2026, 8, 13, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr("pizzeria_dashboard.dashboard._now", lambda: fixed_now)
    client = app.test_client()
    board = client.get("/?date=2026-08-13")
    html = board.get_data(as_text=True)
    pie_key = re.search(r'data-bake-timer-key="([^"]+)"', html).group(1)

    started = client.post(
        "/pie-production-state",
        json={
            "service_date": "2026-08-13",
            "pie_key": pie_key,
            "timer_action": "start",
        },
    )
    assert started.status_code == 200
    assert started.get_json()["pies"][pie_key]["oven_position"] == "top-left"

    finished = client.post(
        "/pie-production-state",
        json={
            "service_date": "2026-08-13",
            "pie_key": pie_key,
            "timer_action": "finish",
        },
    )
    assert finished.status_code == 200
    assert finished.get_json()["pies"][pie_key]["timer_status"] == "done"
    assert finished.get_json()["pies"][pie_key]["oven_position"] is None


def test_customer_can_be_marked_vip_and_star_appears_on_order_card(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()
    selected = date(2026, 7, 31)
    assert client.get(f"/?date={selected.isoformat()}").status_code == 200
    order = load_orders_for_date(Path(app.config["DATABASE_PATH"]), selected)[0]

    details = client.get(
        f"/order-details?date={selected.isoformat()}&order_id={order.order_id}"
    )
    assert details.status_code == 200
    assert b'data-order-vip-button' in details.data
    assert b'Mark as VIP' in details.data

    saved = client.post(
        "/order-vip",
        json={
            "service_date": selected.isoformat(),
            "order_id": order.order_id,
            "vip": True,
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["vip"] is True

    board = client.get(f"/?date={selected.isoformat()}")
    assert b'class="vip-star"' in board.data
    assert b'VIP customer' in board.data

    removed = client.post(
        "/order-vip",
        json={
            "service_date": selected.isoformat(),
            "order_id": order.order_id,
            "vip": False,
        },
    )
    assert removed.status_code == 200
    board = client.get(f"/?date={selected.isoformat()}")
    assert b'class="vip-star"' not in board.data


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
    assert b'class="visit-medal visit-medal--tier-3"' in dashboard.data
    assert b'aria-label="5"' in dashboard.data
    assert b'<span class="visit-medal-number">5</span>' in dashboard.data
    assert b"Customer visits" in dashboard.data
    assert b"14 online orders" in dashboard.data
    assert b"Walk-in pie orders" in dashboard.data
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


def test_non_pizza_online_order_renders_main_items_and_drink_tag(tmp_path: Path) -> None:
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 7, 31)
    order = Order(
        order_id="non-pizza-online",
        customer_name="Alex R.",
        pickup_at=datetime(2026, 7, 31, 16, 0),
        items=(
            Item("Cucumber Salad", 1, "salad"),
            Item("Mari T-Shirt", 1, "merch"),
            Item("Mexican Coke", 2, "drink"),
        ),
        square_order_id="non-pizza-online",
        square_version=1,
        fulfillment_uid="pickup-1",
        fulfillment_state="RESERVED",
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected, (order,), source="square"
    )

    response = app.test_client().get(f"/?date={selected.isoformat()}")
    visible_text = _visible_text(response)

    assert response.status_code == 200
    assert "Cucumber Salad" in visible_text
    assert "Mari T-Shirt" in visible_text
    assert "2× Drinks" in visible_text
    assert b'badge--salad' in response.data
    assert b'badge--merch' in response.data
    assert b'badge--drink' in response.data
    order_start = response.data.index(b'data-order-id="non-pizza-online"')
    order_end = response.data.index(b'</div>', order_start)
    order_prefix = response.data[order_start:order_end]
    assert b'class="item-name">Cucumber Salad' not in order_prefix
    assert b'class="item-name">Mari T-Shirt' not in order_prefix
    # A zero-pizza slot with a production order is not an empty Prep View slot.
    order_position = response.data.index(b'data-order-id="non-pizza-online"')
    slot_start = response.data.rfind(b'class="pickup-window', 0, order_position)
    slot_end = response.data.index(b'</article>', order_position)
    slot_html = response.data[slot_start:slot_end]
    assert b'data-slot-empty="false"' in slot_html
    assert b'data-order-pizza-units="0"' in slot_html


def test_main_salad_side_cookie_and_merch_items_are_badges_not_pizza_lines(tmp_path: Path) -> None:
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 8, 13)
    order = Order(
        order_id="mixed-main-items",
        customer_name="Alex R.",
        pickup_at=datetime(2026, 8, 13, 16, 0),
        items=(
            Item("Plain Pie", 1, "pizza"),
            Item("Cucumber Salad", 1, "salad"),
            Item("Side Ranch", 1, "side"),
            Item("TCHO Miso Chocolate Chip Cookie", 2, "cookie"),
            Item("Mari T-Shirt", 1, "merch"),
        ),
        square_order_id="mixed-main-items",
        square_version=1,
        fulfillment_uid="pickup-1",
        fulfillment_state="RESERVED",
    )
    replace_orders_for_date(
        Path(app.config["DATABASE_PATH"]), selected, (order,), source="square"
    )

    response = app.test_client().get(f"/?date={selected.isoformat()}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'badge--salad">1× Cucumber Salad' in html
    assert 'badge--side">1× Side Ranch' in html
    assert 'badge--cookie">2× Cookie' in html
    assert 'badge--merch">1× Mari T-Shirt' in html
    assert 'class="item-name">Plain Pie' in html
    assert 'class="item-name">Cucumber Salad' not in html
    assert 'class="item-name">Side Ranch' not in html
    assert 'class="item-name">TCHO Miso Chocolate Chip Cookie' not in html
    assert 'class="item-name">Mari T-Shirt' not in html


def test_manual_order_can_be_added_without_square_and_appears_on_board(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            8,
            12,
            19,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/manual-order",
        data={
            "customer_name": "Phone Alex",
            "pickup_date": "2026-08-14",
            "pickup_time": "18:15",
            "item_quantity": ["1", "1", "2"],
            "item_name": ["Plain Pie", "Pizzeria Mari Tee", "Mexican Coke"],
            "item_category": ["pizza", "merch", "drink"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/?date=2026-08-14")

    database_path = Path(app.config["DATABASE_PATH"])
    cached = load_orders_for_date(database_path, date(2026, 8, 14))
    assert len(cached) == 1
    manual = cached[0]
    assert manual.is_manual is True
    assert manual.square_order_id is None
    assert manual.customer_name == "Phone Alex"
    assert manual.pickup_at == datetime(2026, 8, 14, 18, 15)
    assert [(item.name, item.quantity, item.category) for item in manual.items] == [
        ("Plain Pie", 1, "pizza"),
        ("Pizzeria Mari Tee", 1, "merch"),
        ("Mexican Coke", 2, "drink"),
    ]

    board = client.get("/?date=2026-08-14")
    html = board.get_data(as_text=True)
    assert board.status_code == 200
    assert "Phone Alex" in html
    assert ">Manual<" in html
    assert "1× Plain Pie" in html
    assert "1× Pizzeria Mari Tee" in html
    assert "2× Drinks" in html
    assert "UNPAID — DO NOT PREP" not in html
    assert "data-bake-timer" in html


def test_manual_order_form_is_available_from_dashboard(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-08-14")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Add order" in html
    assert 'id="manual-order-dialog"' in html
    assert 'action="/manual-order"' in html
    assert "Dashboard only" in html
    assert 'name="customer_name"' in html
    assert 'name="pickup_date"' in html
    assert 'name="pickup_time"' in html
    assert 'name="item_name"' in html
    assert 'name="item_quantity"' in html
    assert 'name="item_category"' in html


def test_manual_order_requires_at_least_one_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(
            2026,
            8,
            12,
            19,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ),
    )
    app = _test_app(tmp_path)
    response = app.test_client().post(
        "/manual-order",
        data={
            "customer_name": "Phone Alex",
            "pickup_date": "2026-08-14",
            "pickup_time": "18:15",
            "item_quantity": ["1"],
            "item_name": [""],
            "item_category": ["pizza"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert load_orders_for_date(
        Path(app.config["DATABASE_PATH"]), date(2026, 8, 14)
    ) == ()


def test_remove_from_dashboard_hides_source_order_without_square_mutation(tmp_path: Path, monkeypatch) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    app = _test_app(tmp_path)
    client = app.test_client()
    selected = date(2026, 7, 31)
    assert client.get(f"/?date={selected.isoformat()}").status_code == 200
    database_path = Path(app.config["DATABASE_PATH"])
    target = load_orders_for_date(database_path, selected)[0]

    class ExplodingSquareClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Removing from dashboard must not instantiate SquareClient")

    monkeypatch.setattr(dashboard_module, "SquareClient", ExplodingSquareClient)
    response = client.post(
        "/order-remove-local",
        json={"service_date": selected.isoformat(), "order_id": target.order_id},
    )
    assert response.status_code == 200
    assert response.get_json()["square_unchanged"] is True
    assert target.order_id not in {order.order_id for order in load_orders_for_date(database_path, selected)}

    # Re-seeding/replacing source data does not resurrect the locally hidden order.
    replace_orders_for_date(database_path, selected, build_sample_orders(selected), source="sample")
    assert target.order_id not in {order.order_id for order in load_orders_for_date(database_path, selected)}


def test_manual_order_can_be_deleted_from_dashboard_only(tmp_path: Path, monkeypatch) -> None:
    import pizzeria_dashboard.dashboard as dashboard_module

    monkeypatch.setattr(
        dashboard_module,
        "_now",
        lambda: datetime(2026, 8, 12, 19, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    app = _test_app(tmp_path)
    client = app.test_client()
    client.post(
        "/manual-order",
        data={
            "customer_name": "Delete Me",
            "pickup_date": "2026-08-14",
            "pickup_time": "18:15",
            "item_quantity": ["1"],
            "item_name": ["Plain Pie"],
            "item_category": ["pizza"],
        },
    )
    selected = date(2026, 8, 14)
    database_path = Path(app.config["DATABASE_PATH"])
    manual = next(order for order in load_orders_for_date(database_path, selected) if order.is_manual)

    details = client.get(f"/order-details?date={selected.isoformat()}&order_id={manual.order_id}")
    assert details.status_code == 200
    assert b"Remove from dashboard" in details.data
    assert b"No Square order is involved" in details.data

    response = client.post(
        "/order-remove-local",
        json={"service_date": selected.isoformat(), "order_id": manual.order_id},
    )
    assert response.status_code == 200
    assert response.get_json()["removal_type"] == "manual"
    assert load_orders_for_date(database_path, selected) == ()


def test_primary_salad_gets_same_green_card_behavior_as_salad_modifier_and_drinks_are_counted(tmp_path: Path) -> None:
    from datetime import datetime
    from pizzeria_dashboard.database import replace_orders_for_date
    from pizzeria_dashboard.domain import Item, Order

    app = _test_app(tmp_path, AUTO_SEED_SAMPLE_DATA=False)
    selected = date(2026, 8, 13)
    order = Order(
        order_id="legacy-salad-drinks",
        customer_name="Alex R.",
        pickup_at=datetime(2026, 8, 13, 16, 0),
        items=(
            Item("Cucumber Salad", 1, "other"),
            Item("Mexican Coke", 2, "drink"),
            Item("Sparkling Water", 1, "drink"),
        ),
        square_order_id="legacy-salad-drinks",
        square_version=1,
        fulfillment_uid="pickup-1",
        fulfillment_state="RESERVED",
    )
    replace_orders_for_date(Path(app.config["DATABASE_PATH"]), selected, (order,), source="square")

    response = app.test_client().get(f"/?date={selected.isoformat()}")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'order-row--salad' in html
    assert 'badge--salad">1× Cucumber Salad' in html
    assert 'badge--drink">3× Drinks' in html
    assert 'class="item-name">Cucumber Salad' not in html
    assert 'class="item-name">Mexican Coke' not in html

    details = app.test_client().get(
        "/order-details",
        query_string={"date": selected.isoformat(), "order_id": order.order_id},
    )
    detail_html = details.get_data(as_text=True)
    assert details.status_code == 200
    assert "2× Mexican Coke" in detail_html
    assert "1× Sparkling Water" in detail_html


def test_boxed_action_sits_left_of_timer_stack_and_customer_visit_medal_is_pizza_themed(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    today = datetime.now(ZoneInfo(app.config["SERVICE_TIMEZONE"])).date()
    response = app.test_client().get(f"/?date={today.isoformat()}")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    controls_pos = html.find('class="pizza-bake-controls"')
    boxed_pos = html.find('order-boxed-inline', controls_pos)
    stack_pos = html.find('class="pizza-bake-stack"', controls_pos)
    timer_pos = html.find('data-bake-timer', stack_pos)
    assert controls_pos != -1
    assert controls_pos < boxed_pos < stack_pos < timer_pos
    css = Path("pizzeria_dashboard/static/style.css").read_text()
    assert ".visit-medal" in css
    assert "--crust: #b86f2c;" in css
    assert "--cheese: #f3cd54;" in css
    assert "--sauce: #b33b2f;" in css
    assert "--basil: #33803d;" in css
    assert ".visit-medal--tier-7" in css
    assert ".visit-medal-number" in css
    assert "background: #f2e5d8;" in css


def test_pizzas_all_day_decrements_when_timer_starts_or_boxed_as_fallback() -> None:
    javascript = Path("pizzeria_dashboard/static/dashboard.js").read_text()
    template = Path("pizzeria_dashboard/templates/_production_board.html").read_text()
    assert "isDoughCommittedTimerStatus" in javascript
    assert 'status === "running" || status === "paused" || status === "done"' in javascript
    assert "timerCommittedPizzaLines" in javascript
    assert "timer.dataset.timerLineKey || key" in javascript
    assert 'timer.dataset.timerLineQuantity || "1"' in javascript
    assert "committedLines.has(lineKey)" in javascript
    assert "committed += quantity" in javascript
    assert "committedPizzaCounts" in javascript
    assert "committedPizzaUnitCount" in javascript
    assert 'data-timer-line-key="{{ pie_base_key }}"' in template
    assert 'data-timer-line-quantity="{{ item.quantity }}"' in template
    assert 'parseOrderCounts(row, "orderPizzaCounts")' in javascript
    assert "Math.max(units - timedUnits, 0)" in javascript
    assert "Math.max(orderUnits - timedUnits, 0)" in javascript
    assert "Math.max(total - committed, 0)" in javascript
    assert "Math.max(fullCount - consumedCount, 0)" in javascript


def test_done_timer_rail_can_be_dismissed_per_device() -> None:
    javascript = Path("pizzeria_dashboard/static/dashboard.js").read_text()
    assert "pizzeria-dashboard:dismissed-done-timers:" in javascript
    assert "Click to dismiss" in javascript
    assert "dismissedDoneTimers.add(key)" in javascript
    assert "window.localStorage" in javascript


def test_customer_visit_medal_is_next_to_customer_name_and_capacity_action_is_not_on_main_card(tmp_path: Path) -> None:
    response = _test_app(tmp_path).test_client().get("/?date=2026-07-31")
    html = response.get_data(as_text=True)
    # The visit medal belongs in the customer-name wrapper, before the generic
    # order-badge collection used for production/status markers.
    customer_wrap = html.find('class="customer-name-wrap"')
    visit_medal = html.find('class="visit-medal', customer_wrap)
    order_badges = html.find('class="order-badges"', customer_wrap)
    if visit_medal != -1:
        assert customer_wrap < visit_medal < order_badges
    assert 'class="order-capacity-control"' not in html


def test_zero_pizza_finale_rains_pizza_slices_for_one_minute():
    javascript = Path("pizzeria_dashboard/static/dashboard.js").read_text()
    css = Path("pizzeria_dashboard/static/style.css").read_text()

    assert "PIZZA_RAIN_DURATION_MS = 60000" in javascript
    assert 'slice.textContent = "🍕"' in javascript
    assert 'rain.className = "pizza-finale-rain"' in javascript
    assert 'slice.className = "pizza-finale-slice"' in javascript
    assert "pointer-events: none" in css
    assert "@keyframes pizza-finale-rain-fall" in css
    assert ".pizza-finale-rain" in css
    assert "prefers-reduced-motion: reduce" in css
