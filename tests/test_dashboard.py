import json
from pathlib import Path

from pizzeria_dashboard import create_app
from pizzeria_dashboard.sample_data import build_sample_service
from pizzeria_dashboard.service_state import build_inventory_summary, default_state


def _test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "SERVICE_STATE_PATH": str(tmp_path / "service_state.json"),
        }
    )


def test_dashboard_renders_multiple_orders_and_pizza_totals(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Pizzeria Mari" in response.data
    assert b"Production board" in response.data
    assert b"Tomato Pie" in response.data
    assert b"3 pizzas" in response.data
    assert response.data.count(b'class="order-row') >= 3


def test_release_candidates_consider_total_slot_capacity() -> None:
    service = build_sample_service()
    candidates = {
        order.order_id
        for window in service.windows
        for order in window.orders
        if service.is_release_candidate(order, window)
    }

    assert candidates == {"PM-1047", "PM-1053"}
    assert service.release_candidates == 2
    assert [window.pickup_at.strftime("%-I:%M %p") for window in service.release_candidate_windows] == [
        "4:30 PM",
        "5:00 PM",
    ]


def test_release_candidate_card_lists_open_slot_times(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/")

    assert b"Release candidates" in response.data
    assert b"4:30 PM" in response.data
    assert b"5:00 PM" in response.data
    assert b"1 pizza space open" in response.data


def test_release_candidate_tags_render_only_for_eligible_orders(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/")

    assert response.data.count(b"Release candidate") == 3  # Card label + two tags.
    assert b"Lee C." in response.data  # One pie, but in a full three-pizza slot.


def test_modifiers_render_with_their_pizza_items(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/")

    assert b"Plain Pie" in response.data
    assert b"Pepperoni" in response.data
    assert b"White Pie" in response.data
    assert b"Pickled chiles" in response.data
    assert b"Basil" in response.data
    assert b'aria-label="Modifiers for White Pie"' in response.data


def test_salads_are_modifiers_and_counted_by_type(tmp_path: Path) -> None:
    service = build_sample_service()
    inventory = build_inventory_summary(service, default_state())
    stock = {salad.name: salad for salad in inventory.salads}

    assert service.salad_counts["Cucumber Salad"] == 3
    assert service.salad_counts["Kale Caesar Salad"] == 3
    assert stock["Cucumber Salad"].ordered == 3
    assert stock["Kale Caesar Salad"].ordered == 3

    app = _test_app(tmp_path)
    response = app.test_client().get("/")

    assert b"+ 1\xc3\x97 Cucumber Salad" in response.data
    assert b"+ 2\xc3\x97 Kale Caesar Salad" in response.data
    assert b"Salad order" in response.data
    assert b"item-row--salad" not in response.data


def test_order_numbers_are_not_rendered(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    response = app.test_client().get("/")

    assert b"PM-1042" not in response.data
    assert b"order-id" not in response.data


def test_drinks_are_hidden_from_production_view(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/")

    assert b"Mexican Coke" not in response.data
    assert b"Sparkling Water" not in response.data
    assert b"Jamie Q." not in response.data


def test_inventory_defaults_show_dough_remaining(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    response = app.test_client().get("/")

    assert b"Dough inventory" in response.data
    assert b"dough balls remaining" in response.data
    assert b"24" in response.data
    assert b"16" in response.data
    assert b">8<" in response.data


def test_inventory_inputs_persist_to_json(tmp_path: Path) -> None:
    state_path = tmp_path / "service_state.json"
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/inventory",
        data={
            "service_date": "2026-07-31",
            "dough_balls_prepared": "30",
            "salad_cucumber_salad": "12",
            "salad_kale_caesar_salad": "9",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    payload = json.loads(state_path.read_text())
    assert payload == {
        "version": 1,
        "services": {
            "2026-07-31": {
                "dough_balls_prepared": 30,
                "salad_prepared": {
                    "Cucumber Salad": 12,
                    "Kale Caesar Salad": 9,
                },
            }
        },
    }
    assert b'value="30"' in response.data
    assert b'value="12"' in response.data
    assert b'value="9"' in response.data


def test_sample_service_groups_two_or_three_orders_per_window() -> None:
    service = build_sample_service()

    assert any(window.order_count == 3 for window in service.windows)
    assert any(window.order_count == 2 for window in service.windows)
    assert all(window.order_count in {2, 3} for window in service.windows)


def test_sync_redirects_to_dashboard(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.post("/sync", data={"service_date": "2026-07-24"})

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/?date=2026-07-24")


def test_selected_service_date_changes_board_date(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    response = app.test_client().get("/?date=2026-07-24")

    assert response.status_code == 200
    assert b"Friday, July 24, 2026" in response.data
    assert b'value="2026-07-24"' in response.data
    assert b"Return to today" in response.data


def test_previous_and_next_service_date_links_render(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    response = app.test_client().get("/?date=2026-07-24")

    assert b"date=2026-07-23" in response.data
    assert b"date=2026-07-25" in response.data


def test_inventory_is_stored_separately_by_service_date(tmp_path: Path) -> None:
    state_path = tmp_path / "service_state.json"
    app = _test_app(tmp_path)
    client = app.test_client()

    client.post(
        "/inventory",
        data={
            "service_date": "2026-07-24",
            "dough_balls_prepared": "30",
            "salad_cucumber_salad": "12",
            "salad_kale_caesar_salad": "9",
        },
    )
    client.post(
        "/inventory",
        data={
            "service_date": "2026-07-25",
            "dough_balls_prepared": "40",
            "salad_cucumber_salad": "7",
            "salad_kale_caesar_salad": "6",
        },
    )

    payload = json.loads(state_path.read_text())
    assert payload["services"]["2026-07-24"]["dough_balls_prepared"] == 30
    assert payload["services"]["2026-07-25"]["dough_balls_prepared"] == 40

    first_day = client.get("/?date=2026-07-24")
    second_day = client.get("/?date=2026-07-25")
    assert b'value="30"' in first_day.data
    assert b'value="40"' in second_day.data


def test_health_endpoint(tmp_path: Path) -> None:
    app = _test_app(tmp_path)
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
