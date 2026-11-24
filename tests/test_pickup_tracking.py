from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import re
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pizzeria_dashboard import create_app
from pizzeria_dashboard.customer_history import CustomerHistoryOrder
from pizzeria_dashboard.database import (
    initialize_database, link_manual_order_square_payment, load_arrival_patterns, load_board_content_revision,
    load_pickup_marks, remove_order_from_dashboard, replace_customer_history,
    merge_customer_history, replace_orders_for_date, reschedule_cached_order, save_manual_order,
    save_order_slot_assignment, save_square_customer_profiles,
)
from pizzeria_dashboard.domain import Item, Order
from pizzeria_dashboard.pickup_tracking import PickupMark, arrival_pattern, timing_difference


ZONE = ZoneInfo("America/New_York")
SLOT = datetime(2026, 8, 6, 17, tzinfo=ZONE)
DAY = SLOT.date()


@pytest.fixture
def counter(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=SLOT)
    monkeypatch.setattr("pizzeria_dashboard.dashboard._now", lambda: clock.now)
    path = tmp_path / "dashboard.db"
    app = create_app({
        "TESTING": True, "SECRET_KEY": "counter-test", "DATABASE_PATH": str(path),
        "LEGACY_SERVICE_STATE_PATH": str(tmp_path / "legacy.json"), "ORDER_SOURCE": "sample",
        "AUTO_SEED_SAMPLE_DATA": False, "DASHBOARD_AUTH_USERNAME": "", "DASHBOARD_AUTH_PASSWORD": "",
        "SERVICE_TIMEZONE": ZONE.key,
    })
    return app.test_client(), path, clock


def order(key, at=SLOT, **kwargs):
    return Order(key, "Sam Lee", at, (Item("Plain Pie", 1, "pizza"),),
                 square_order_id=f"sq-{key}", **kwargs)


def tap(client, ticket, event, **extra):
    return client.post("/order-pickup-event", json={
        "service_date": ticket.pickup_at.date().isoformat(),
        "order_id": ticket.order_id, "event": event, **extra,
    })


def card(html, order_id):
    return next(row for row in re.split(r'<div\s+class="order-row\b', html)[1:]
                if f'data-order-id="{order_id}"' in row)


@pytest.mark.parametrize("days_ahead", [0, 3])
def test_order_actions_share_one_row_per_order_with_or_without_pies(counter, days_ahead):
    client, path, _ = counter
    at = SLOT + timedelta(days=days_ahead)
    multi = replace(order("multi", at), items=(Item("Plain Pie", 2, "pizza"), Item("White Pie", 1, "pizza")))
    salad = replace(order("salad", at, is_walk_in=True), items=(Item("Caesar Salad", 1, "salad"),))
    replace_orders_for_date(path, at.date(), (multi, salad), source="square")
    page = client.get(f"/?date={at.date()}").get_data(as_text=True)

    for ticket in (multi, salad):
        row = card(page, ticket.order_id)
        groups = re.findall(r'<div class="order-action-row"[^>]*>(.*?)</div>', row, re.S)
        assert len(groups) == 1
        buttons = re.findall(r'<button\b[^>]*>.*?</button>', groups[0], re.S)
        assert len(buttons) == 3
        for button, hook, label in zip(buttons, ("data-order-boxed-button", 'data-pickup-event="arrived"',
                                                 'data-pickup-event="picked_up"'),
                                       ("Mark Boxed", "Mark Arrived", "Mark Picked Up"), strict=True):
            assert row.count(hook) == 1 and hook in button
            assert f">{label}</span>" in button and 'class="badge badge--order-action"' in button
        assert row.count("data-bake-timer-toggle") == ticket.pizza_units
        assert row.index('class="customer-name"') < row.index('class="order-action-row"') < row.index('class="item-list"')


@pytest.mark.parametrize(("minutes", "category"), [
    (-6, "early"), (-5, "on-time"), (0, "on-time"), (5, "on-time"), (6, "late"),
])
def test_arrival_classification_by_displayed_minute(minutes, category):
    actual = SLOT + timedelta(minutes=minutes, seconds=45)
    assert timing_difference(actual, SLOT) == (minutes, category)


def test_midnight_and_dst_are_compared_as_instants():
    fall = datetime(2026, 11, 1, 1, 30, tzinfo=ZONE)
    assert timing_difference(fall.replace(fold=1), fall) == (60, "late")
    midnight = SLOT.replace(hour=0, minute=2)
    mark = PickupMark(midnight - timedelta(minutes=7), midnight).view(DAY, ZONE)
    assert mark["clock"] == "Aug 5, 11:55 PM" and mark["offset"] == "7 min early"


def test_marks_survive_refresh_and_history_is_read_only(counter):
    client, path, clock = counter
    ticket, missed = order("first"), order("missed")
    replace_orders_for_date(path, DAY, (ticket, missed), source="square")
    revision = load_board_content_revision(path, DAY)
    page = client.get(f"/?date={DAY}").get_data(as_text=True)
    assert page.count('data-pickup-event="arrived"') == page.count('data-pickup-event="picked_up"') == 2
    assert 'src="/static/pickup-tracking.js?v=2"' in page
    clock.now = SLOT - timedelta(minutes=8)
    arrived = tap(client, ticket, "arrived").get_json()["marks"]["arrived"]
    assert arrived["clock"] == "4:52 PM" and arrived["offset"] == "8 min early"
    clock.now = SLOT + timedelta(minutes=11)
    assert tap(client, ticket, "arrived").get_json()["marks"]["arrived"] == arrived
    both = tap(client, ticket, "picked_up").get_json()["marks"]
    assert both["picked_up"]["offset"] == "11 min late" and both["arrived"] == arrived
    assert load_board_content_revision(path, DAY) == revision  # no board reload on other displays
    live = client.get(f"/live-production-state?date={DAY}").get_json()
    assert live["pickup_marks"][ticket.order_id] == both
    assert not live["boxed_orders"] and not live["pies"]
    replace_orders_for_date(path, DAY, (ticket, missed), source="square")
    clock.now += timedelta(days=1)
    historical = client.get(f"/?date={DAY}").get_data(as_text=True)
    assert "8 min early" in card(historical, "first") and "11 min late" in card(historical, "first")
    assert 'data-pickup-event="' not in historical
    missed = card(historical, "missed")
    assert "Mark Arrived" in missed and "Mark Picked Up" in missed and "Not recorded" in missed
    assert ">1 early</span>" in historical and ">0 late</span>" in historical
    assert tap(client, ticket, "arrived", undo_recorded_at=arrived["recorded_at"]).status_code == 409


def test_pickup_can_be_recorded_without_inventing_arrival_and_undo_is_guarded(counter):
    client, path, clock = counter
    ticket = order("one")
    replace_orders_for_date(path, DAY, (ticket,), source="square")
    mark = tap(client, ticket, "picked_up").get_json()["marks"]
    assert set(mark) == {"picked_up"}
    assert tap(client, ticket, "arrived").status_code == 409
    stamp = mark["picked_up"]["recorded_at"]
    assert tap(client, ticket, "picked_up", undo_recorded_at=stamp).get_json()["marks"] == {}
    assert tap(client, ticket, "arrived").status_code == 200
    clock.now += timedelta(minutes=2)
    newer = tap(client, ticket, "picked_up").get_json()["marks"]["picked_up"]
    assert tap(client, ticket, "picked_up", undo_recorded_at=stamp).status_code == 409
    assert tap(client, ticket, "picked_up").get_json()["marks"]["picked_up"] == newer


def test_two_registers_record_only_one_arrival(counter):
    client, path, _ = counter
    ticket = order("simultaneous")
    replace_orders_for_date(path, DAY, (ticket,), source="square")
    def mark(_):
        return tap(client.application.test_client(), ticket, "arrived").get_json()["marks"]
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(mark, range(2))
    assert first == second and set(load_pickup_marks(path, DAY)[ticket.order_id]) == {"arrived"}


def test_effective_schedule_is_snapshotted_not_recomputed(counter):
    client, path, clock = counter
    walkin = order("walkin", is_walk_in=True)
    timed = order("ticket-time", is_walk_in=True, ticket_name="5:15 Sam")
    moved = order("rescheduled")
    replace_orders_for_date(path, DAY, (walkin, timed, moved), source="square")
    assert tap(client, walkin, "arrived").get_json()["marks"]["arrived"]["status"] == "unscheduled"
    assert tap(client, timed, "arrived").get_json()["marks"]["arrived"]["offset"] == "15 min early"
    custom = SLOT.replace(hour=14, minute=7, tzinfo=None)
    save_order_slot_assignment(path, DAY, moved.order_id, custom)
    clock.now = SLOT.replace(hour=14, minute=9)
    arrival = tap(client, moved, "arrived").get_json()["marks"]["arrived"]
    assert arrival["schedule"] == "Scheduled for 2:07 PM" and arrival["offset"] == "On time · 2 min late"
    save_order_slot_assignment(path, DAY, moved.order_id, SLOT.replace(tzinfo=None))
    tomorrow = replace(moved, pickup_at=SLOT + timedelta(days=1))
    reschedule_cached_order(path, DAY, tomorrow.pickup_at.date(), tomorrow)
    new = client.get(f"/live-production-state?date={tomorrow.pickup_at.date()}").get_json()["pickup_marks"]
    assert new["rescheduled"]["arrived"]["offset"] == arrival["offset"]
    assert new["rescheduled"]["arrived"]["clock"] == "Aug 6, 2:09 PM"
    assert "rescheduled" not in load_pickup_marks(path, DAY)
    remove_order_from_dashboard(path, tomorrow.pickup_at.date(), tomorrow.order_id)
    assert load_pickup_marks(path, tomorrow.pickup_at.date()) == {}


def test_unpaid_unknown_and_invalid_writes_leave_no_marks(counter):
    client, path, _ = counter
    unpaid = order("decline", is_paid=False)
    pending = order("pending", reference_id="PMOC-pending", is_paid=None)
    replace_orders_for_date(path, DAY, (unpaid, pending), source="square")
    assert 'data-pickup-event="' not in client.get(f"/?date={DAY}").get_data(as_text=True)
    assert tap(client, unpaid, "arrived").status_code == tap(client, pending, "picked_up").status_code == 400
    assert tap(client, order("absent"), "arrived").status_code == 404
    assert tap(client, unpaid, "typo").status_code == 400
    assert tap(client, unpaid, "arrived", service_date="invalid").status_code == 400
    assert load_pickup_marks(path, DAY) == {}


@pytest.mark.parametrize(("visits", "expected"), [
    (["early"] * 2, None), (["early", "on-time", "late"], None),
    (["early", "early", "late"], "Usually early"), (["late"] * 3, "Usually late"),
    (["on-time"] * 3, "Usually on time"), (["early"] * 3 + ["late"] * 2, None),
])
def test_usually_badges_need_a_two_thirds_majority(visits, expected):
    assert (arrival_pattern(visits) or {}).get("label") == expected


def test_future_tag_uses_verified_profiles_prior_dates_and_late_payment_links(counter):
    client, path, clock = counter
    old = [order(f"old-{n}", SLOT - timedelta(days=n)) for n in (3, 2, 1)]
    old[2] = replace(old[2], creation_product="MANUAL_DASHBOARD", square_order_id=None)
    repeat, same_name, guest = order("return"), order("different"), order("guest")
    def history(ticket, cid, key=None):
        return CustomerHistoryOrder(cid, key or ticket.square_order_id, ticket.pickup_at,
                                    ticket.pickup_at.date(), "Square", ticket.items)
    current_history = [history(repeat, "canonical"), history(same_name, "other-person")]
    replace_customer_history(path, [history(o, "old-id") for o in old[:2]] + current_history,
                             synced_at=SLOT, start_at=SLOT - timedelta(days=10), payment_count=4)
    save_square_customer_profiles(path, ({"_requested_customer_id": "old-id", "id": "canonical"},))
    for i, prior in enumerate(old):
        day = prior.pickup_at.date()
        if prior.is_manual:
            save_manual_order(path, day, prior)
        else:
            replace_orders_for_date(path, day, (prior,), source="square")
        clock.now = prior.pickup_at + timedelta(minutes=-10 if i < 2 else 10)
        assert tap(client, prior, "arrived").status_code == 200
    clock.now = SLOT
    replace_orders_for_date(path, DAY, (repeat, same_name, guest), source="square")
    assert "Usually early" not in client.get(f"/?date={DAY}").get_data(as_text=True)
    # The manual customer is linked only once the ticket is paid. Rebuilding
    # the history index need not include old rows captured with a confirmed ID.
    link_manual_order_square_payment(path, old[2].pickup_at.date(), old[2].order_id,
        square_order_id="manual-payment", square_receipt_number=None, square_ticket_name=None,
        item_discrepancy=False)
    replace_customer_history(path, current_history + [history(old[2], "canonical", "manual-payment")],
                             synced_at=SLOT, start_at=SLOT - timedelta(days=10), payment_count=3)
    board = client.get(f"/?date={DAY}").get_data(as_text=True)
    assert "Usually early" in card(board, "return") and "2 of 3" in card(board, "return")
    assert "Usually early" not in card(board, "different") and "Usually early" not in card(board, "guest")
    assert not load_arrival_patterns(path, old[2].pickup_at.date(), ["canonical"])
    replace_customer_history(path, current_history, synced_at=SLOT, start_at=SLOT, payment_count=2)
    assert tap(client, repeat, "picked_up").status_code == 200
    assert load_arrival_patterns(path, DAY + timedelta(days=1), ["canonical"])["canonical"]["basis"].startswith("2 of 3")


def test_trend_is_recent_distinct_receipts_not_pickups_or_unscheduled_times(counter):
    client, path, clock = counter
    receipts = []
    for n in range(1, 16):
        slot = SLOT - timedelta(days=n)
        ticket = order(f"visit-{n}", slot, is_walk_in=(n == 1))
        # 1 = unscheduled arrival; 2 = pickup without arrival. The next ten
        # scheduled arrivals are seven late, three early; older early visits
        # must not dilute a recent tendency.
        tickets = [ticket]
        if n == 3:
            tickets.append(replace(ticket, order_id="second-fulfillment"))
        replace_orders_for_date(path, slot.date(), tickets, source="square")
        receipts.append(CustomerHistoryOrder("C", ticket.square_order_id, slot, slot.date(), None, ticket.items))
        for item in tickets:
            clock.now = slot + timedelta(minutes=10 if n <= 9 else -10)
            assert tap(client, item, "picked_up" if n == 2 else "arrived").status_code == 200
    merge_customer_history(path, receipts, synced_at=SLOT, start_at=SLOT - timedelta(days=20), payment_count=15)
    assert load_arrival_patterns(path, DAY, ["C"])["C"] == {
        "status": "late", "label": "Usually late", "basis": "7 of 10 prior recorded arrivals were late.",
    }
    # Undo one of the ten on its editable service date: six late out of ten
    # is mixed and must suppress the badge.
    changed = order("visit-4", SLOT - timedelta(days=4))
    clock.now = changed.pickup_at + timedelta(minutes=15)
    token = load_pickup_marks(path, clock.now.date())[changed.order_id]["arrived"].recorded_at.isoformat()
    assert tap(client, changed, "arrived", undo_recorded_at=token).status_code == 200
    assert not load_arrival_patterns(path, DAY, ["C"])


def test_schema_upgrade_does_not_turn_existing_boxed_times_into_pickups(tmp_path):
    path = tmp_path / "v17.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE app_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO app_metadata VALUES ('schema_version', '17');
            CREATE TABLE order_ready_states(service_date TEXT, order_id TEXT, boxed_at TEXT, updated_at TEXT,
                                            PRIMARY KEY(service_date, order_id));
            INSERT INTO order_ready_states VALUES ('2026-08-06', 'A', '2026-08-06T21:00:00+00:00', 'old');
        """)
    initialize_database(path)
    initialize_database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT order_id FROM order_ready_states").fetchall() == [("A",)]
        assert db.execute("SELECT value FROM app_metadata WHERE key='schema_version'").fetchone() == ("20",)
    assert load_pickup_marks(path, DAY) == {}

