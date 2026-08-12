import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from pizzeria_dashboard.database import (
    delete_order_slot_assignment,
    initialize_database,
    load_board_content_revision,
    load_order_ready_states,
    load_order_slot_assignment_overrides,
    load_order_slot_assignments,
    load_order_for_date,
    load_order_internal_note,
    load_order_internal_notes_for_date,
    load_orders_for_date,
    load_pie_production_states,
    load_latest_service_state_before,
    load_service_state_payload,
    load_sync_info,
    merge_orders_for_date,
    migrate_legacy_service_state,
    prune_pie_production_states,
    replace_orders_for_date,
    save_manual_order,
    save_order_internal_note,
    save_order_ready_state,
    save_order_slot_assignment,
    save_service_state_payload,
    update_pie_production_state,
)
from pizzeria_dashboard.domain import Item, Order
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
        "manual_orders",
        "service_states",
        "sync_runs",
        "app_metadata",
        "order_slot_assignments",
        "order_internal_notes",
        "pie_production_states",
        "order_ready_states",
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



def test_manual_order_survives_square_snapshot_replacement(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 14)
    initialize_database(database_path)
    square_orders = build_sample_orders(service_date)
    manual = Order(
        order_id="manual-test-1",
        customer_name="Phone Alex",
        pickup_at=datetime(2026, 8, 14, 18, 15),
        items=(
            Item(name="Plain Pie", quantity=1, category="pizza"),
            Item(name="T-Shirt", quantity=1, category="merch"),
        ),
        creation_product="MANUAL_DASHBOARD",
    )

    save_manual_order(database_path, service_date, manual)
    save_order_slot_assignment(
        database_path,
        service_date,
        manual.order_id,
        datetime(2026, 8, 14, 18, 30),
    )
    replace_orders_for_date(
        database_path, service_date, square_orders, source="sample"
    )

    cached = load_orders_for_date(database_path, service_date)
    assert manual in cached
    assert len(cached) == len(square_orders) + 1
    assert load_order_for_date(database_path, service_date, manual.order_id) == manual
    assert load_order_slot_assignments(database_path, service_date)[manual.order_id] == datetime(2026, 8, 14, 18, 30)

    replace_orders_for_date(
        database_path, service_date, square_orders[:2], source="sample"
    )
    cached = load_orders_for_date(database_path, service_date)
    assert manual in cached
    assert len(cached) == 3


def test_internal_order_notes_survive_order_refresh_and_can_be_cleared(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)
    orders = build_sample_orders(service_date)
    replace_orders_for_date(database_path, service_date, orders, source="sample")

    saved = save_order_internal_note(
        database_path,
        service_date,
        orders[0].order_id,
        "  Allergy: use clean cutter.\r\nSubstitute basil.  ",
    )

    assert saved == "Allergy: use clean cutter.\nSubstitute basil."
    assert load_order_internal_note(
        database_path, service_date, orders[0].order_id
    ) == "Allergy: use clean cutter.\nSubstitute basil."
    assert load_order_internal_notes_for_date(database_path, service_date) == {
        orders[0].order_id: "Allergy: use clean cutter.\nSubstitute basil."
    }

    replace_orders_for_date(database_path, service_date, orders, source="sample")
    assert load_order_internal_note(
        database_path, service_date, orders[0].order_id
    ) == "Allergy: use clean cutter.\nSubstitute basil."

    assert save_order_internal_note(
        database_path, service_date, orders[0].order_id, "   "
    ) is None
    assert load_order_internal_note(
        database_path, service_date, orders[0].order_id
    ) is None


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


def test_scheduled_pickup_override_can_be_restored_to_source_time(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 7, 31)
    initialize_database(database_path)

    save_order_slot_assignment(
        database_path,
        service_date,
        "scheduled-1",
        datetime(2026, 7, 31, 17, 15),
    )
    assert load_order_slot_assignments(database_path, service_date) == {
        "scheduled-1": datetime(2026, 7, 31, 17, 15)
    }

    delete_order_slot_assignment(database_path, service_date, "scheduled-1")
    assert load_order_slot_assignment_overrides(database_path, service_date) == {}


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


def test_latest_service_state_before_returns_most_recent_saved_day(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    save_service_state_payload(database_path, date(2026, 8, 7), {"cookie_prepared": 4})
    save_service_state_payload(database_path, date(2026, 8, 8), {"cookie_prepared": 2})

    previous = load_latest_service_state_before(database_path, date(2026, 8, 9))

    assert previous == (date(2026, 8, 8), {"cookie_prepared": 2})


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



def test_order_cache_changes_publish_board_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    service_date = date(2026, 8, 7)
    first = Order(
        "order-1",
        "A",
        datetime(2026, 8, 7, 16, 0),
        (Item("Plain Pie", 1, "pizza"),),
    )
    second = Order(
        "order-2",
        "B",
        datetime(2026, 8, 7, 16, 15),
        (Item("White Pie", 1, "pizza"),),
    )

    replace_orders_for_date(database_path, service_date, (first,), source="sample")
    first_revision = load_board_content_revision(database_path, service_date)
    assert first_revision

    merge_orders_for_date(
        database_path,
        service_date,
        (second,),
        candidate_square_order_ids=("order-2",),
        source="square",
    )
    second_revision = load_board_content_revision(database_path, service_date)
    assert second_revision
    assert second_revision != first_revision

def test_noop_full_order_refresh_preserves_board_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    service_date = date(2026, 8, 7)
    order = Order(
        "order-1",
        "A",
        datetime(2026, 8, 7, 16, 0),
        (Item("Plain Pie", 1, "pizza"),),
    )

    replace_orders_for_date(database_path, service_date, (order,), source="sample")
    save_order_internal_note(database_path, service_date, "order-1", "Allergy")
    note_revision = load_board_content_revision(database_path, service_date)

    replace_orders_for_date(database_path, service_date, (order,), source="sample")

    assert load_board_content_revision(database_path, service_date) == note_revision


def test_shared_pie_timer_auto_assigns_oven_positions_and_can_reset(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)
    first_key = "2026-08-06|order-1|plain|0"
    second_key = "2026-08-06|order-2|white|0"

    first = update_pie_production_state(
        database_path, service_date, first_key, timer_action="start", duration_ms=480_000
    )
    second = update_pie_production_state(
        database_path, service_date, second_key, timer_action="start", duration_ms=480_000
    )

    assert first.timer_status == "running"
    assert first.oven_position == "top-left"
    assert second.oven_position == "top-right"

    paused = update_pie_production_state(
        database_path, service_date, first_key, timer_action="pause", duration_ms=480_000
    )
    assert paused.timer_status == "paused"
    assert 0 < paused.timer_remaining_ms <= 480_000

    reset = update_pie_production_state(
        database_path, service_date, first_key, timer_action="reset", duration_ms=480_000
    )
    assert reset.timer_status == "idle"
    assert reset.oven_position is None


def test_completed_timer_frees_oven_position_for_next_pie(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)
    first_key = "2026-08-06|order-1|plain|0"
    second_key = "2026-08-06|order-2|white|0"

    first = update_pie_production_state(
        database_path, service_date, first_key, timer_action="start", duration_ms=1_000
    )
    assert first.oven_position == "top-left"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE pie_production_states
            SET timer_end_at_ms = 1
            WHERE service_date = ? AND pie_key = ?
            """,
            (service_date.isoformat(), first_key),
        )

    completed = load_pie_production_states(database_path, service_date)[first_key]
    assert completed.timer_status == "done"
    assert completed.oven_position is None

    second = update_pie_production_state(
        database_path, service_date, second_key, timer_action="start", duration_ms=480_000
    )
    assert second.oven_position == "top-left"


def test_manual_oven_assignment_replaces_existing_occupant(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)
    first_key = "2026-08-06|order-1|plain|0"
    second_key = "2026-08-06|order-2|white|0"
    update_pie_production_state(
        database_path, service_date, first_key, oven_position="bottom-left"
    )
    update_pie_production_state(
        database_path, service_date, second_key, oven_position="bottom-left"
    )

    states = load_pie_production_states(database_path, service_date)
    assert states[first_key].oven_position is None
    assert states[second_key].oven_position == "bottom-left"


def test_shared_boxed_ready_state_can_be_set_and_cleared(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)

    boxed_at = save_order_ready_state(
        database_path, service_date, "order-1", boxed=True
    )
    assert boxed_at is not None
    assert load_order_ready_states(database_path, service_date)["order-1"] == boxed_at

    save_order_ready_state(database_path, service_date, "order-1", boxed=False)
    assert load_order_ready_states(database_path, service_date) == {}


def test_board_content_revision_changes_when_note_is_cleared(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)

    save_order_internal_note(database_path, service_date, "order-1", "Allergy")
    first_revision = load_board_content_revision(database_path, service_date)
    save_order_internal_note(database_path, service_date, "order-2", "Substitution")
    second_revision = load_board_content_revision(database_path, service_date)
    save_order_internal_note(database_path, service_date, "order-1", "")
    cleared_revision = load_board_content_revision(database_path, service_date)

    assert first_revision
    assert len({first_revision, second_revision, cleared_revision}) == 3


def test_stale_pie_states_are_pruned_to_current_board_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    service_date = date(2026, 8, 6)
    initialize_database(database_path)
    current_key = "2026-08-06|order-1|plain|0"
    stale_key = "2026-08-06|removed|plain|0"
    update_pie_production_state(database_path, service_date, current_key, oven_position="top-left")
    update_pie_production_state(database_path, service_date, stale_key, oven_position="top-right")

    assert prune_pie_production_states(database_path, service_date, (current_key,)) == 1
    assert set(load_pie_production_states(database_path, service_date)) == {current_key}
