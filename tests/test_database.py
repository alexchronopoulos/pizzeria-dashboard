import json
import sqlite3
from datetime import date
from pathlib import Path

from pizzeria_dashboard.database import (
    initialize_database,
    load_order_slot_assignment_overrides,
    load_order_slot_assignments,
    load_order_for_date,
    load_orders_for_date,
    load_service_state_payload,
    load_sync_info,
    merge_orders_for_date,
    migrate_legacy_service_state,
    replace_orders_for_date,
    save_order_slot_assignment,
    save_service_state_payload,
)
from pizzeria_dashboard.sample_data import build_sample_orders


def test_database_initializes_expected_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "orders",
        "service_states",
        "sync_runs",
        "app_metadata",
        "order_slot_assignments",
    } <= tables


def test_order_documents_round_trip_through_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    original = build_sample_orders(service_date)

    replace_orders_for_date(database_path, service_date, original, source="sample")
    cached = load_orders_for_date(database_path, service_date)

    assert cached == original
    assert cached[1].items[0].modifiers[0].name == "Pepperoni"
    assert cached[3].items[0].modifiers[0].category == "salad"
    assert cached[0].square_order_id is None
    assert cached[0].receipt_number == "FCMu"


def test_one_cached_order_can_be_loaded_for_details(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    original = build_sample_orders(service_date)
    replace_orders_for_date(database_path, service_date, original, source="sample")

    loaded = load_order_for_date(database_path, service_date, original[0].order_id)

    assert loaded == original[0]
    assert load_order_for_date(database_path, service_date, "missing") is None


def test_replacing_a_date_removes_stale_cached_orders(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    orders = build_sample_orders(service_date)

    replace_orders_for_date(database_path, service_date, orders, source="sample")
    replace_orders_for_date(database_path, service_date, orders[:2], source="sample")

    assert load_orders_for_date(database_path, service_date) == orders[:2]
    sync_info = load_sync_info(database_path, service_date)
    assert sync_info is not None
    assert sync_info.order_count == 2


def test_walk_in_slot_assignments_survive_sync_and_prune_stale_orders(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    orders = build_sample_orders(service_date)
    replace_orders_for_date(database_path, service_date, orders, source="sample")

    assigned_at = datetime(2026, 7, 31, 16, 15)
    save_order_slot_assignment(
        database_path, service_date, orders[0].order_id, assigned_at
    )
    replace_orders_for_date(database_path, service_date, orders, source="sample")
    assert load_order_slot_assignments(database_path, service_date) == {
        orders[0].order_id: assigned_at
    }

    replace_orders_for_date(database_path, service_date, orders[1:], source="sample")
    assert load_order_slot_assignments(database_path, service_date) == {}


def test_separate_service_dates_remain_cached(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    first_date = date(2026, 7, 31)
    second_date = date(2026, 8, 1)
    initialize_database(database_path)

    replace_orders_for_date(
        database_path, first_date, build_sample_orders(first_date), source="sample"
    )
    replace_orders_for_date(
        database_path, second_date, build_sample_orders(second_date), source="sample"
    )

    first = load_orders_for_date(database_path, first_date)
    second = load_orders_for_date(database_path, second_date)
    assert len(first) == len(second) == 14
    assert first[0].pickup_at.date() == first_date
    assert second[0].pickup_at.date() == second_date
    assert first[0].order_id != second[0].order_id


def test_service_state_round_trips_as_json_document(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    payload = {
        "dough_balls_prepared": 31,
        "salad_prepared": {"Cucumber Salad": 10, "Kale Caesar Salad": 9},
        "future_field": {"can_change_without_migration": True},
    }

    save_service_state_payload(database_path, service_date, payload)

    assert load_service_state_payload(database_path, service_date) == payload


def test_legacy_date_based_json_state_migrates_once(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    legacy_path = tmp_path / "service_state.json"
    initialize_database(database_path)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "services": {
                    "2026-07-31": {
                        "dough_balls_prepared": 30,
                        "salad_prepared": {"Cucumber Salad": 12},
                    }
                },
            }
        )
    )

    assert migrate_legacy_service_state(database_path, legacy_path) == 1
    assert migrate_legacy_service_state(database_path, legacy_path) == 0
    payload = load_service_state_payload(database_path, date(2026, 7, 31))
    assert payload is not None
    assert payload["dough_balls_prepared"] == 30


def test_explicit_unscheduled_override_is_preserved_separately(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)

    save_order_slot_assignment(database_path, service_date, "walk-in-1", None)

    assert load_order_slot_assignments(database_path, service_date) == {}
    assert load_order_slot_assignment_overrides(database_path, service_date) == {
        "walk-in-1": None
    }


def test_incremental_merge_preserves_untouched_orders_and_removes_changed_candidates(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    original = build_sample_orders(service_date)[:3]
    square_orders = tuple(
        replace(order, square_order_id=f"square-{index}")
        for index, order in enumerate(original, start=1)
    )
    replace_orders_for_date(
        database_path, service_date, square_orders, source="square"
    )

    updated = replace(square_orders[0], customer_name="Updated customer")
    result = merge_orders_for_date(
        database_path,
        service_date,
        (updated,),
        candidate_square_order_ids=("square-1", "square-2"),
        source="square",
        synced_at=datetime(2026, 7, 31, 18, 0, tzinfo=UTC),
    )

    cached = load_orders_for_date(database_path, service_date)
    assert [order.square_order_id for order in cached] == ["square-1", "square-3"]
    assert next(order for order in cached if order.square_order_id == "square-1").customer_name == "Updated customer"
    assert result.changed_count == 1
    assert result.removed_count == 1
    assert result.info.order_count == 2
