from datetime import UTC, datetime
from pathlib import Path

from pizzeria_dashboard.customer_history import CustomerHistoryOrder, CustomerSummary
from pizzeria_dashboard.customer_history_sync import sync_customer_history
from pizzeria_dashboard.database import (
    initialize_database,
    load_customer_history_for_order,
    load_customer_history_sync_info,
    load_customer_summaries_for_orders,
    merge_customer_history,
    replace_customer_history,
)
from pizzeria_dashboard.domain import Item, Modifier


def _history_order(
    customer_id: str,
    order_id: str,
    day: int,
    item_name: str = "Plain Pie",
) -> CustomerHistoryOrder:
    ordered_at = datetime(2026, 7, day, 20, 0, tzinfo=UTC)
    return CustomerHistoryOrder(
        customer_id=customer_id,
        order_id=order_id,
        ordered_at=ordered_at,
        service_date=ordered_at.date(),
        source="Square Online",
        items=(
            Item(
                item_name,
                1,
                "pizza",
                modifiers=(Modifier("Basil"),),
            ),
        ),
    )


def test_customer_summary_labels() -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    last = datetime(2026, 7, 1, tzinfo=UTC)

    assert CustomerSummary("c", 1, first, last).tag_label == "First Timer"
    assert CustomerSummary("c", 2, first, last).tag_label == "2nd Order"
    assert CustomerSummary("c", 3, first, last).tag_label == "3rd Order"
    assert CustomerSummary("c", 4, first, last).tag_label == "4th Order"
    assert CustomerSummary("c", 5, first, last).tag_label == "Regular · 5 orders"


def test_customer_history_round_trips_and_maps_current_order(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    orders = tuple(
        _history_order("customer-1", f"order-{number}", number)
        for number in range(1, 7)
    )
    replace_customer_history(
        database_path,
        orders,
        synced_at=datetime(2026, 8, 2, tzinfo=UTC),
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        payment_count=6,
    )

    summaries = load_customer_summaries_for_orders(database_path, ("order-6",))
    assert summaries["order-6"].tag_label == "Regular · 6 orders"

    history = load_customer_history_for_order(database_path, "order-6")
    assert history is not None
    assert history.summary.order_count == 6
    assert history.orders[0].order_id == "order-6"
    assert history.orders[0].display_items[0].name == "Plain Pie"
    assert history.orders[0].display_items[0].production_modifiers[0].name == "Basil"


def test_incremental_customer_history_merge_updates_existing_order(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    initial = _history_order("customer-1", "order-1", 1)
    replace_customer_history(
        database_path,
        (initial,),
        synced_at=datetime(2026, 7, 2, tzinfo=UTC),
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        payment_count=1,
    )

    changed = _history_order("customer-1", "order-1", 1, "Collar City")
    added = _history_order("customer-1", "order-2", 2, "Cherry Tomato")
    info, changed_count = merge_customer_history(
        database_path,
        (changed, added),
        synced_at=datetime(2026, 7, 3, tzinfo=UTC),
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        payment_count=2,
    )

    assert changed_count == 2
    assert info.order_count == 2
    history = load_customer_history_for_order(database_path, "order-2")
    assert history is not None
    assert history.summary.tag_label == "2nd Order"
    assert {entry.items[0].name for entry in history.orders} == {
        "Collar City",
        "Cherry Tomato",
    }


class _FakeSquareClient:
    def resolve_location(self):
        return {"id": "LOCATION-1", "name": "Pizzeria Mari"}

    def list_payments(self, **kwargs):
        return (
            {
                "id": "payment-1",
                "status": "COMPLETED",
                "order_id": "order-1",
                "customer_id": "customer-1",
                "created_at": "2026-07-01T20:00:00Z",
            },
            {
                "id": "payment-2",
                "status": "COMPLETED",
                "order_id": "order-2",
                "customer_id": "customer-1",
                "created_at": "2026-07-10T20:00:00Z",
            },
            {
                "id": "anonymous-payment",
                "status": "COMPLETED",
                "order_id": "anonymous-order",
                "created_at": "2026-07-11T20:00:00Z",
            },
        )

    def batch_retrieve_orders(self, order_ids, *, location_id=None):
        return tuple(
            {
                "id": order_id,
                "state": "COMPLETED",
                "creation_source": {"name": "Square Online"},
                "line_items": [
                    {
                        "uid": f"line-{order_id}",
                        "name": "Collar City" if order_id == "order-2" else "Plain Pie",
                        "quantity": "1",
                    }
                ],
            }
            for order_id in order_ids
        )

    def batch_retrieve_catalog_objects(self, object_ids, *, include_related_objects=False):
        return ()


def test_full_customer_history_sync_uses_payment_customer_ids(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    result = sync_customer_history(
        database_path,
        {
            "SQUARE_ACCESS_TOKEN": "token",
            "SQUARE_LOCATION_ID": "LOCATION-1",
            "SERVICE_TIMEZONE": "America/New_York",
            "CUSTOMER_HISTORY_START_DATE": "2025-01-01",
        },
        square_client=_FakeSquareClient(),
        full=True,
        force=True,
    )

    assert result is not None
    assert result.info.order_count == 2
    assert result.changed_count == 2
    assert load_customer_history_sync_info(database_path) is not None
    history = load_customer_history_for_order(database_path, "order-2")
    assert history is not None
    assert history.summary.tag_label == "2nd Order"
    assert history.orders[0].items[0].name == "Collar City"
    assert load_customer_history_for_order(database_path, "anonymous-order") is None
