from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping

import pytest

from pizzeria_dashboard.database import initialize_database, load_orders_for_date
from pizzeria_dashboard.domain import build_service_board
from pizzeria_dashboard.square_api import (
    SquareClient,
    SquareConfigurationError,
    SquareSettings,
)
from pizzeria_dashboard.square_orders import (
    CatalogItemInfo,
    CatalogModifierInfo,
    ClassificationRules,
    build_catalog_index,
    build_modifier_index,
    build_receipt_number_index,
    convert_square_orders,
    service_date_search_range,
)
from pizzeria_dashboard.sync_service import sync_orders_for_date


SERVICE_DATE = date(2026, 7, 31)


def _raw_square_orders() -> tuple[Mapping[str, object], ...]:
    return (
        {
            "id": "square-order-1",
            "location_id": "LOCATION-1",
            "state": "OPEN",
            "version": 7,
            "updated_at": "2026-07-30T18:05:00Z",
            "line_items": [
                {
                    "uid": "line-plain",
                    "catalog_object_id": "variation-plain",
                    "name": "Plain Pie",
                    "quantity": "1",
                    "modifiers": [
                        {
                            "catalog_object_id": "modifier-pepperoni",
                            "name": "Pepperoni",
                            "quantity": "1",
                        },
                        {
                            "catalog_object_id": "modifier-cucumber",
                            "name": "Cucumber Salad",
                            "quantity": "1",
                        },
                        {"name": "Side Hot Honey", "quantity": "1"},
                        {"name": "Cookie", "quantity": "2"},
                    ],
                },
                {
                    "uid": "line-coke",
                    "catalog_object_id": "variation-coke",
                    "name": "Mexican Coke",
                    "quantity": "2",
                },
            ],
            "fulfillments": [
                {
                    "uid": "pickup-1",
                    "type": "PICKUP",
                    "state": "RESERVED",
                    "pickup_details": {
                        "pickup_at": "2026-07-31T20:00:00Z",
                        "recipient": {"display_name": "Alex R."},
                    },
                }
            ],
        },
        {
            "id": "square-order-completed",
            "location_id": "LOCATION-1",
            "state": "COMPLETED",
            "version": 4,
            "updated_at": "2026-07-31T20:20:00Z",
            "line_items": [
                {
                    "uid": "line-white",
                    "catalog_object_id": "variation-white",
                    "name": "White Pie",
                    "quantity": "1",
                    "modifiers": [
                        {"name": "Pickled chiles", "quantity": "1"},
                        {"name": "Basil", "quantity": "1"},
                    ],
                }
            ],
            "fulfillments": [
                {
                    "uid": "pickup-2",
                    "type": "PICKUP",
                    "state": "COMPLETED",
                    "pickup_details": {
                        "pickup_at": "2026-07-31T20:15:00Z",
                        "recipient": {"display_name": "Morgan S."},
                    },
                }
            ],
        },
        {
            "id": "different-service-date",
            "location_id": "LOCATION-1",
            "state": "OPEN",
            "version": 1,
            "line_items": [
                {
                    "uid": "line-other-date",
                    "catalog_object_id": "variation-plain",
                    "name": "Plain Pie",
                    "quantity": "1",
                }
            ],
            "fulfillments": [
                {
                    "uid": "pickup-other-date",
                    "type": "PICKUP",
                    "state": "PROPOSED",
                    "pickup_details": {
                        "pickup_at": "2026-08-01T20:00:00Z",
                        "recipient": {"display_name": "Tomorrow Guest"},
                    },
                }
            ],
        },
    )


def _catalog_index() -> dict[str, CatalogItemInfo]:
    return {
        "variation-plain": CatalogItemInfo(
            "variation-plain", "Plain Pie", "Regular", ("Pizzas",)
        ),
        "variation-white": CatalogItemInfo(
            "variation-white", "White Pie", "Regular", ("Pizzas",)
        ),
        "variation-coke": CatalogItemInfo(
            "variation-coke", "Mexican Coke", "Bottle", ("Drinks",)
        ),
    }


def test_square_orders_are_filtered_by_pickup_date_and_keep_modifiers() -> None:
    orders = convert_square_orders(
        _raw_square_orders(),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        receipt_numbers_by_order_id={"square-order-1": "FCMu"},
        rules=ClassificationRules(),
    )

    assert len(orders) == 2
    first = orders[0]
    assert first.customer_name == "Alex R."
    assert first.pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-31 16:00"
    assert first.square_order_id == "square-order-1"
    assert first.receipt_number == "FCMu"
    assert first.square_version == 7
    assert first.fulfillment_uid == "pickup-1"
    assert first.pizza_units == 1
    assert first.items[0].category == "pizza"
    assert first.items[1].category == "drink"
    assert first.production_items == (first.items[0],)
    assert [modifier.name for modifier in first.items[0].modifiers] == [
        "Pepperoni",
        "Cucumber Salad",
        "Side Hot Honey",
        "Cookie",
    ]
    assert first.items[0].modifiers[1].is_salad
    assert first.items[0].modifiers[2].is_side
    assert first.items[0].modifiers[3].is_cookie
    assert first.salad_counts["Cucumber Salad"] == 1
    assert first.side_counts["Side Hot Honey"] == 1
    assert first.cookie_count == 2

    completed = orders[1]
    assert completed.released is True
    assert completed.fulfillment_state == "COMPLETED"
    assert [modifier.name for modifier in completed.items[0].modifiers] == [
        "Pickled chiles",
        "Basil",
    ]


def test_draft_orders_are_excluded_from_production_orders() -> None:
    draft = dict(_raw_square_orders()[0])
    draft["id"] = "draft-cart"
    draft["state"] = "DRAFT"
    draft["fulfillments"] = [
        {
            "uid": "draft-pickup",
            "type": "PICKUP",
            "state": "PROPOSED",
            "pickup_details": {
                "pickup_at": "2026-07-31T20:30:00Z",
                "recipient": {},
            },
        }
    ]

    orders = convert_square_orders(
        (*_raw_square_orders(), draft),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert all(order.square_order_id != "draft-cart" for order in orders)
    assert len(orders) == 2


def test_receipt_lookup_can_follow_order_tender_payment_id() -> None:
    raw = list(_raw_square_orders())
    first = dict(raw[0])
    first["tenders"] = [{"id": "payment-guest"}]
    raw[0] = first

    orders = convert_square_orders(
        tuple(raw),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        receipt_numbers_by_order_id={"payment-guest": "T9st"},
        rules=ClassificationRules(),
    )

    assert orders[0].receipt_number == "T9st"


def test_search_range_includes_preorders_created_before_service_date() -> None:
    start_at, end_at = service_date_search_range(
        SERVICE_DATE, "America/New_York", 60
    )
    assert start_at == "2026-06-01T04:00:00Z"
    assert end_at == "2026-08-01T04:00:00Z"


def test_square_client_paginates_search_orders() -> None:
    calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        assert headers["Square-Version"] == "2026-07-15"
        assert "secret-token" not in str(payload)
        if payload and payload.get("cursor") == "next-page":
            return {"orders": [{"id": "second"}]}
        return {"orders": [{"id": "first"}], "cursor": "next-page"}

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )
    orders = client.search_pickup_orders(
        location_id="LOCATION-1",
        created_start_at="2026-06-01T04:00:00Z",
        created_end_at="2026-08-01T04:00:00Z",
    )

    assert [order["id"] for order in orders] == ["first", "second"]
    assert len(calls) == 2
    first_payload = calls[0][2]
    assert first_payload is not None
    query = first_payload["query"]
    assert isinstance(query, Mapping)
    filter_payload = query["filter"]
    assert isinstance(filter_payload, Mapping)
    assert filter_payload["state_filter"] == {"states": ["OPEN", "COMPLETED"]}
    assert "fulfillment_filter" not in filter_payload




def test_square_client_searches_incrementally_by_updated_at() -> None:
    calls: list[Mapping[str, object] | None] = []

    def requester(method, url, headers, payload, timeout):
        calls.append(payload)
        return {"orders": [{"id": "changed-order"}]}

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )
    orders = client.search_orders_updated_since(
        location_id="LOCATION-1",
        updated_start_at="2026-07-31T18:00:00Z",
        updated_end_at="2026-07-31T18:05:00Z",
    )

    assert [order["id"] for order in orders] == ["changed-order"]
    payload = calls[0]
    assert payload is not None
    query = payload["query"]
    assert isinstance(query, Mapping)
    filter_payload = query["filter"]
    assert isinstance(filter_payload, Mapping)
    assert "state_filter" not in filter_payload
    assert filter_payload["date_time_filter"] == {
        "updated_at": {
            "start_at": "2026-07-31T18:00:00Z",
            "end_at": "2026-07-31T18:05:00Z",
        }
    }
    assert query["sort"] == {
        "sort_field": "UPDATED_AT",
        "sort_order": "ASC",
    }


def test_completed_orders_without_pickup_times_become_walk_ins() -> None:
    raw_orders = (
        {
            "id": "pos-walk-in-1",
            "location_id": "LOCATION-1",
            "state": "COMPLETED",
            "created_at": "2026-07-31T17:05:00Z",
            "closed_at": "2026-07-31T17:07:00Z",
            "updated_at": "2026-07-31T17:07:00Z",
            "ticket_name": "Ticket 42",
            "creation_source": {
                "product": "SQUARE_POS",
                "name": "Square POS 6.74 for Android",
            },
            "line_items": [
                {
                    "uid": "line-plain",
                    "catalog_object_id": "variation-plain",
                    "name": "Plain Pie",
                    "quantity": "2",
                }
            ],
        },
        {
            "id": "online-no-fulfillment",
            "location_id": "LOCATION-1",
            "state": "COMPLETED",
            "created_at": "2026-07-31T17:10:00Z",
            "closed_at": "2026-07-31T17:11:00Z",
            "creation_source": {
                "product": "ONLINE_STORE",
                "name": "Square Online",
            },
            "line_items": [
                {
                    "uid": "line-online",
                    "catalog_object_id": "variation-plain",
                    "name": "Plain Pie",
                    "quantity": "1",
                }
            ],
        },
    )

    orders = convert_square_orders(
        raw_orders,
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 2
    by_id = {order.square_order_id: order for order in orders}

    pos_order = by_id["pos-walk-in-1"]
    assert pos_order.customer_name == "Ticket 42"
    assert pos_order.is_walk_in is True
    assert pos_order.creation_product == "SQUARE_POS"
    assert pos_order.pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-31 13:07"
    assert pos_order.pizza_units == 2

    # Source labels are intentionally ignored. A completed order with no real
    # pickup timestamp belongs in the unscheduled lane for that service date.
    online_order = by_id["online-no-fulfillment"]
    assert online_order.is_walk_in is True
    assert online_order.pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-31 13:11"


def test_generic_order_with_receipt_becomes_walk_in() -> None:
    raw_order = {
        "id": "generic-order-tqGG",
        "location_id": "LOCATION-1",
        "state": "COMPLETED",
        "created_at": "2026-07-24T20:12:00Z",
        "closed_at": "2026-07-24T20:14:00Z",
        "creation_source": {"name": "Order"},
        "source": {"name": "Order"},
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=date(2026, 7, 24),
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        receipt_numbers_by_order_id={"generic-order-tqGG": "tqGG"},
        paid_order_ids={"generic-order-tqGG"},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    assert orders[0].is_walk_in is True
    assert orders[0].receipt_number == "tqGG"
    assert orders[0].pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-24 16:14"


def test_generic_completed_order_needs_no_payment_or_source_metadata() -> None:
    raw_order = {
        "id": "generic-order-without-payment-metadata",
        "location_id": "LOCATION-1",
        "state": "COMPLETED",
        "created_at": "2026-07-24T20:12:00Z",
        "closed_at": "2026-07-24T20:14:00Z",
        "creation_source": {"name": "Order"},
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=date(2026, 7, 24),
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    assert orders[0].is_walk_in is True
    assert orders[0].receipt_number is None
    assert orders[0].pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-24 16:14"


def test_walk_in_matches_created_date_when_closed_date_is_different() -> None:
    raw_order = {
        "id": "created-on-service-date",
        "state": "COMPLETED",
        "created_at": "2026-07-24T23:58:00Z",
        "closed_at": "2026-07-25T04:03:00Z",
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=date(2026, 7, 24),
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    assert orders[0].pickup_at.strftime("%Y-%m-%d %H:%M") == "2026-07-24 19:58"


def test_pickup_fulfillment_without_pickup_time_is_unscheduled() -> None:
    raw_order = {
        "id": "pickup-without-time",
        "state": "COMPLETED",
        "created_at": "2026-07-31T17:05:00Z",
        "closed_at": "2026-07-31T17:07:00Z",
        "fulfillments": [
            {
                "uid": "pickup-1",
                "type": "PICKUP",
                "state": "COMPLETED",
                "pickup_details": {},
            }
        ],
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    assert orders[0].is_walk_in is True
    assert orders[0].pickup_at.strftime("%-I:%M %p") == "1:07 PM"


def test_generic_order_with_delivery_fulfillment_is_not_a_walk_in() -> None:
    raw_order = {
        "id": "generic-delivery",
        "state": "COMPLETED",
        "created_at": "2026-07-31T17:05:00Z",
        "closed_at": "2026-07-31T17:07:00Z",
        "creation_source": {"name": "Order"},
        "fulfillments": [{"uid": "delivery-1", "type": "DELIVERY"}],
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        paid_order_ids={"generic-delivery"},
        rules=ClassificationRules(),
    )

    assert orders == ()


def test_pos_walk_ins_use_created_time_when_closed_time_is_missing() -> None:
    raw_order = {
        "id": "pos-walk-in-created",
        "state": "COMPLETED",
        "created_at": "2026-07-31T20:05:00Z",
        "creation_source": {"product": "SQUARE_POS"},
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    assert orders[0].customer_name == "Walk-in"
    assert orders[0].pickup_at.strftime("%-I:%M %p") == "4:05 PM"


def test_square_client_paginates_payments_for_receipt_lookup() -> None:
    calls: list[tuple[str, str]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url))
        assert payload is None
        if "cursor=next-page" in url:
            return {
                "payments": [
                    {
                        "id": "payment-2",
                        "order_id": "order-2",
                        "receipt_number": "B2cd",
                    }
                ]
            }
        return {
            "payments": [
                {
                    "id": "payment-1",
                    "order_id": "order-1",
                    "receipt_number": "FCMu",
                }
            ],
            "cursor": "next-page",
        }

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )
    payments = client.list_payments(
        location_id="LOCATION-1",
        begin_time="2026-06-01T04:00:00Z",
        end_time="2026-08-01T04:00:00Z",
    )

    assert [payment["receipt_number"] for payment in payments] == ["FCMu", "B2cd"]
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert "/v2/payments?" in calls[0][1]
    assert "location_id=LOCATION-1" in calls[0][1]
    assert "begin_time=2026-06-01T04%3A00%3A00Z" in calls[0][1]


def test_receipt_number_index_ignores_unlinked_or_missing_receipts() -> None:
    index = build_receipt_number_index(
        (
            {"order_id": "order-1", "receipt_number": "FCMu"},
            {"order_id": "order-1", "receipt_number": "later"},
            {"order_id": "order-2"},
            {"receipt_number": "orphan"},
        )
    )
    assert index == {"order-1": "FCMu"}


def test_catalog_index_maps_variations_to_parent_categories() -> None:
    class FakeClient:
        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            if "variation-plain" in object_ids:
                return (
                    {
                        "type": "ITEM_VARIATION",
                        "id": "variation-plain",
                        "item_variation_data": {
                            "item_id": "item-plain",
                            "name": "Regular",
                        },
                    },
                    {
                        "type": "ITEM",
                        "id": "item-plain",
                        "item_data": {
                            "name": "Plain Pie",
                            "kitchen_name": "PLAIN",
                            "categories": [{"id": "category-pizza"}],
                            "reporting_category": {"id": "category-reporting"},
                        },
                    },
                )
            assert set(object_ids) == {"category-pizza", "category-reporting"}
            return (
                {
                    "type": "CATEGORY",
                    "id": "category-pizza",
                    "category_data": {"name": "Traditional Pies"},
                },
                {
                    "type": "CATEGORY",
                    "id": "category-reporting",
                    "category_data": {"name": "Mari Pies"},
                },
            )

    index = build_catalog_index(FakeClient(), _raw_square_orders()[:1])
    assert index["variation-plain"].item_name == "Plain Pie"
    assert index["variation-plain"].kitchen_name == "PLAIN"
    assert index["variation-plain"].variation_name == "Regular"
    assert index["variation-plain"].category_names == (
        "Traditional Pies",
        "Mari Pies",
    )


def test_kitchen_names_drive_item_and_modifier_display_and_classification() -> None:
    rules = ClassificationRules(
        pizza_category_names=(),
        pizza_item_keywords=("pizza", "pie"),
    )
    orders = convert_square_orders(
        _raw_square_orders()[:1],
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index={
            "variation-plain": CatalogItemInfo(
                catalog_object_id="variation-plain",
                item_name="Plain Pie",
                variation_name="Regular",
                category_names=("Seasonal Special Pies",),
                kitchen_name="PLAIN",
            ),
            "variation-coke": CatalogItemInfo(
                catalog_object_id="variation-coke",
                item_name="Mexican Coke",
                variation_name="Bottle",
                category_names=("Drinks",),
                kitchen_name="COKE",
            ),
        },
        modifier_index={
            "modifier-pepperoni": CatalogModifierInfo(
                catalog_object_id="modifier-pepperoni",
                name="Pepperoni",
                kitchen_name="ADD PEPP",
            ),
            "modifier-cucumber": CatalogModifierInfo(
                catalog_object_id="modifier-cucumber",
                name="Cucumber Salad",
                kitchen_name="CUCUMBER SALAD",
            ),
        },
        rules=rules,
    )

    order = orders[0]
    assert order.items[0].name == "PLAIN"
    assert order.items[0].category == "pizza"
    assert order.pizza_units == 1
    assert [modifier.name for modifier in order.items[0].modifiers] == [
        "ADD PEPP",
        "CUCUMBER SALAD",
        "Side Hot Honey",
        "Cookie",
    ]
    assert order.items[0].modifiers[1].is_salad
    assert order.items[1].name == "COKE"
    assert order.items[1].category == "drink"


def test_modifier_index_reads_kitchen_names() -> None:
    class FakeClient:
        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            assert set(object_ids) == {
                "modifier-pepperoni",
                "modifier-cucumber",
            }
            return (
                {
                    "type": "MODIFIER",
                    "id": "modifier-pepperoni",
                    "modifier_data": {
                        "name": "Pepperoni",
                        "kitchen_name": "ADD PEPP",
                    },
                },
                {
                    "type": "MODIFIER",
                    "id": "modifier-cucumber",
                    "modifier_data": {
                        "name": "Cucumber Salad",
                        "kitchen_name": "CUC SALAD",
                    },
                },
            )

    index = build_modifier_index(FakeClient(), _raw_square_orders()[:1])
    assert index["modifier-pepperoni"].kitchen_name == "ADD PEPP"
    assert index["modifier-cucumber"].kitchen_name == "CUC SALAD"


def test_completed_guest_orders_are_omitted_but_active_guests_remain() -> None:
    base_line_items = [
        {
            "uid": "line-plain",
            "catalog_object_id": "variation-plain",
            "name": "Plain Pie",
            "quantity": "1",
        }
    ]
    raw_orders = (
        {
            "id": "completed-guest",
            "state": "COMPLETED",
            "line_items": base_line_items,
            "fulfillments": [
                {
                    "uid": "completed-pickup",
                    "type": "PICKUP",
                    "state": "COMPLETED",
                    "pickup_details": {"pickup_at": "2026-07-31T20:00:00Z"},
                }
            ],
        },
        {
            "id": "active-guest",
            "state": "OPEN",
            "line_items": base_line_items,
            "fulfillments": [
                {
                    "uid": "active-pickup",
                    "type": "PICKUP",
                    "state": "RESERVED",
                    "pickup_details": {"pickup_at": "2026-07-31T20:15:00Z"},
                }
            ],
        },
    )

    orders = convert_square_orders(
        raw_orders,
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert [order.square_order_id for order in orders] == ["active-guest"]
    assert orders[0].customer_name == "Guest"


def test_square_sync_writes_live_orders_to_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)

    class FakeSquareClient:
        settings = SquareSettings("token", "LOCATION-1", order_lookback_days=60)

        def resolve_location(self):
            return {
                "id": "LOCATION-1",
                "name": "Pizzeria Mari",
                "timezone": "America/New_York",
            }

        def search_pickup_orders(self, **kwargs):
            draft = dict(_raw_square_orders()[0])
            draft["id"] = "draft-cart"
            draft["state"] = "DRAFT"
            return (*_raw_square_orders(), draft)

        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            # Force name-based fallbacks for this integration test.
            return ()

    result = sync_orders_for_date(
        database_path,
        SERVICE_DATE,
        {
            "ORDER_SOURCE": "square",
            "SQUARE_ACCESS_TOKEN": "token",
            "SQUARE_LOCATION_ID": "LOCATION-1",
            "SERVICE_TIMEZONE": "America/New_York",
            "SQUARE_ORDER_LOOKBACK_DAYS": 60,
        },
        square_client=FakeSquareClient(),
    )

    assert result.info.source == "square"
    assert result.info.order_count == 2
    assert result.candidates_scanned == 4
    cached = load_orders_for_date(database_path, SERVICE_DATE)
    assert all(order.square_order_id != "draft-cart" for order in cached)
    assert cached[0].square_order_id == "square-order-1"
    assert cached[0].receipt_number is None
    assert cached[0].items[0].catalog_object_id == "variation-plain"


def test_square_sync_includes_paid_generic_order_source_walk_in(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)

    class FakeSquareClient:
        settings = SquareSettings("token", "LOCATION-1", order_lookback_days=60)

        def resolve_location(self):
            return {
                "id": "LOCATION-1",
                "name": "Pizzeria Mari",
                "timezone": "America/New_York",
            }

        def search_orders_for_service_date(self, **kwargs):
            return (
                {
                    "id": "generic-order-L1so",
                    "location_id": "LOCATION-1",
                    "state": "COMPLETED",
                    "created_at": "2026-07-31T18:02:00Z",
                    "closed_at": "2026-07-31T18:04:00Z",
                    "creation_source": {"name": "Order"},
                    "source": {"name": "Order"},
                    "line_items": [
                        {
                            "uid": "line-plain",
                            "catalog_object_id": "variation-plain",
                            "name": "Plain Pie",
                            "quantity": "1",
                        }
                    ],
                },
            )

        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            return ()

    result = sync_orders_for_date(
        database_path,
        SERVICE_DATE,
        {
            "ORDER_SOURCE": "square",
            "SQUARE_ACCESS_TOKEN": "token",
            "SQUARE_LOCATION_ID": "LOCATION-1",
            "SERVICE_TIMEZONE": "America/New_York",
            "SQUARE_ORDER_LOOKBACK_DAYS": 60,
        },
        square_client=FakeSquareClient(),
    )

    assert result.info.order_count == 1
    cached = load_orders_for_date(database_path, SERVICE_DATE)
    assert len(cached) == 1
    assert cached[0].is_walk_in is True
    assert cached[0].receipt_number is None
    assert cached[0].pickup_at.strftime("%-I:%M %p") == "2:04 PM"


def test_square_sync_includes_completed_order_without_payment_metadata(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)

    class FakeSquareClient:
        settings = SquareSettings("token", "LOCATION-1", order_lookback_days=60)

        def resolve_location(self):
            return {
                "id": "LOCATION-1",
                "name": "Pizzeria Mari",
                "timezone": "America/New_York",
            }

        def search_orders_for_service_date(self, **kwargs):
            return (
                {
                    "id": "generic-order-no-payment",
                    "location_id": "LOCATION-1",
                    "state": "COMPLETED",
                    "created_at": "2026-07-31T18:02:00Z",
                    "closed_at": "2026-07-31T18:04:00Z",
                    "creation_source": {"name": "Order"},
                    "line_items": [
                        {
                            "uid": "line-plain",
                            "catalog_object_id": "variation-plain",
                            "name": "Plain Pie",
                            "quantity": "1",
                        }
                    ],
                },
            )

        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            return ()

    result = sync_orders_for_date(
        database_path,
        SERVICE_DATE,
        {
            "ORDER_SOURCE": "square",
            "SQUARE_ACCESS_TOKEN": "token",
            "SQUARE_LOCATION_ID": "LOCATION-1",
            "SERVICE_TIMEZONE": "America/New_York",
            "SQUARE_ORDER_LOOKBACK_DAYS": 60,
        },
        square_client=FakeSquareClient(),
    )

    assert result.info.order_count == 1
    cached = load_orders_for_date(database_path, SERVICE_DATE)
    assert len(cached) == 1
    assert cached[0].is_walk_in is True
    assert cached[0].receipt_number is None
    assert cached[0].pickup_at.strftime("%-I:%M %p") == "2:04 PM"


def test_square_location_must_be_selected_when_multiple_are_active() -> None:
    def requester(method, url, headers, payload, timeout):
        return {
            "locations": [
                {"id": "A", "name": "First", "status": "ACTIVE"},
                {"id": "B", "name": "Second", "status": "ACTIVE"},
            ]
        }

    client = SquareClient(SquareSettings("token", None), requester=requester)
    with pytest.raises(SquareConfigurationError, match="More than one"):
        client.resolve_location()


def test_cookie_categories_and_names_are_classified_for_kitchen_alerts() -> None:
    rules = ClassificationRules(
        cookie_category_names=("Cookies",),
        cookie_item_keywords=("cookie",),
    )
    assert rules.classify_item("Real Fudgy", ("Cookies",)) == "cookie"
    assert rules.classify_item("Chocolate Chip Cookie", ()) == "cookie"


def test_square_client_retrieves_full_order_and_payment_documents() -> None:
    calls: list[tuple[str, str]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url))
        assert payload is None
        if "/v2/orders/" in url:
            return {"order": {"id": "order/with spaces", "state": "OPEN"}}
        return {
            "payment": {
                "id": "payment/with spaces",
                "receipt_number": "FCMu",
            }
        }

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )

    order = client.retrieve_order("order/with spaces")
    payment = client.get_payment("payment/with spaces")

    assert order["state"] == "OPEN"
    assert payment["receipt_number"] == "FCMu"
    assert calls == [
        ("GET", "https://connect.squareup.com/v2/orders/order%2Fwith%20spaces"),
        ("GET", "https://connect.squareup.com/v2/payments/payment%2Fwith%20spaces"),
    ]


def test_mixed_walk_in_keeps_whole_pie_and_marks_slice_non_production() -> None:
    raw_order = {
        "id": "walk-in-pie-and-slice",
        "state": "COMPLETED",
        "created_at": "2026-07-31T20:05:00Z",
        "closed_at": "2026-07-31T20:07:00Z",
        "line_items": [
            {
                "uid": "line-pie",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            },
            {
                "uid": "line-slice",
                "catalog_object_id": "variation-plain-slice",
                "name": "Plain Slice",
                "quantity": "2",
            },
        ],
    }
    catalog_index = {
        **_catalog_index(),
        # Square reporting categories can overlap. The explicit item name and
        # Slice category must win over a broader pie reporting category.
        "variation-plain-slice": CatalogItemInfo(
            "variation-plain-slice",
            "Plain Slice",
            "Regular",
            ("Slices", "Mari Pies"),
        ),
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=SERVICE_DATE,
        timezone_name="America/New_York",
        catalog_index=catalog_index,
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    order = orders[0]
    assert order.is_walk_in is True
    assert [item.category for item in order.items] == ["pizza", "slice"]
    assert [item.name for item in order.production_items] == ["Plain Pie"]
    assert order.pizza_units == 1


def test_slice_name_overrides_shared_pizza_reporting_category() -> None:
    rules = ClassificationRules()

    assert rules.classify_item("Plain Slice", ("Mari Pies",)) == "slice"
    assert rules.classify_item("Plain Pie", ("Mari Pies",)) == "pizza"


def test_ticket_name_walk_in_uses_eastern_timestamps_and_auto_places_745() -> None:
    raw_order = {
        "id": "J73GALemxrWOt5Ehrn7yJHDAjOVZY",
        "location_id": "LOCATION-1",
        "state": "COMPLETED",
        "created_at": "2026-07-24T23:42:00Z",
        "closed_at": "2026-07-24T23:44:00Z",
        "updated_at": "2026-07-24T23:44:01Z",
        "ticket_name": "Sam 7:45 PM",
        "line_items": [
            {
                "uid": "line-plain",
                "catalog_object_id": "variation-plain",
                "name": "Plain Pie",
                "quantity": "1",
            }
        ],
    }

    orders = convert_square_orders(
        (raw_order,),
        service_date=date(2026, 7, 24),
        timezone_name="America/New_York",
        catalog_index=_catalog_index(),
        modifier_index={},
        rules=ClassificationRules(),
    )

    assert len(orders) == 1
    order = orders[0]
    assert order.ticket_name == "Sam 7:45 PM"
    assert order.source_created_at == datetime.fromisoformat(
        "2026-07-24T19:42:00-04:00"
    )
    assert order.source_closed_at == datetime.fromisoformat(
        "2026-07-24T19:44:00-04:00"
    )
    assert order.source_updated_at == datetime.fromisoformat(
        "2026-07-24T19:44:01-04:00"
    )

    slots = tuple(
        datetime(2026, 7, 24, 16, 0) + timedelta(minutes=15 * index)
        for index in range(16)
    )
    board = build_service_board(
        date(2026, 7, 24),
        orders,
        pickup_times=slots,
    )

    target = next(window for window in board.windows if window.pickup_at.hour == 19 and window.pickup_at.minute == 45)
    assert target.orders == (order,)
    assert board.unscheduled_orders == ()


def test_square_client_batch_retrieves_orders_in_one_request() -> None:
    calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        return {"orders": [{"id": value} for value in payload["order_ids"]]}

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )
    orders = client.batch_retrieve_orders(
        ("order-1", "order-2"), location_id="LOCATION-1"
    )

    assert [order["id"] for order in orders] == ["order-1", "order-2"]
    assert calls == [
        (
            "POST",
            "https://connect.squareup.com/v2/orders/batch-retrieve",
            {
                "order_ids": ["order-1", "order-2"],
                "location_id": "LOCATION-1",
            },
        )
    ]


def test_full_sync_prefilters_unrelated_dates_before_walk_in_enrichment(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    batch_ids: list[str] = []

    class FakeSquareClient:
        settings = SquareSettings("token", "LOCATION-1", order_lookback_days=60)

        def resolve_location(self):
            return {"id": "LOCATION-1", "name": "Pizzeria Mari"}

        def search_orders_for_service_date(self, **kwargs):
            return (
                {
                    "id": "old-walk-in",
                    "state": "COMPLETED",
                    "created_at": "2026-07-01T18:00:00Z",
                    "closed_at": "2026-07-01T18:05:00Z",
                    "line_items": [{"name": "Plain Pie", "quantity": "1"}],
                },
                {
                    "id": "today-walk-in",
                    "state": "COMPLETED",
                    "created_at": "2026-07-31T18:00:00Z",
                    "closed_at": "2026-07-31T18:05:00Z",
                    "line_items": [{"name": "Plain Pie", "quantity": "1"}],
                },
            )

        def batch_retrieve_orders(self, order_ids, *, location_id=None):
            batch_ids.extend(order_ids)
            return (
                {
                    "id": "today-walk-in",
                    "state": "COMPLETED",
                    "created_at": "2026-07-31T18:00:00Z",
                    "closed_at": "2026-07-31T18:05:00Z",
                    "ticket_name": "Sam 7:45",
                    "line_items": [{"name": "Plain Pie", "quantity": "1"}],
                },
            )

        def batch_retrieve_catalog_objects(
            self, object_ids, *, include_related_objects=False
        ):
            return ()

    result = sync_orders_for_date(
        database_path,
        SERVICE_DATE,
        {
            "ORDER_SOURCE": "square",
            "SQUARE_ACCESS_TOKEN": "token",
            "SQUARE_LOCATION_ID": "LOCATION-1",
            "SERVICE_TIMEZONE": "America/New_York",
            "SQUARE_ORDER_LOOKBACK_DAYS": 60,
        },
        square_client=FakeSquareClient(),
    )

    assert result.candidates_scanned == 2
    assert batch_ids == ["today-walk-in"]
    cached = load_orders_for_date(database_path, SERVICE_DATE)
    assert [order.square_order_id for order in cached] == ["today-walk-in"]
    assert cached[0].ticket_name == "Sam 7:45"


def test_square_client_completes_pickup_fulfillment_and_order() -> None:
    calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        if method == "GET":
            return {
                "order": {
                    "id": "order-1",
                    "state": "OPEN",
                    "version": 7,
                    "fulfillments": [
                        {
                            "uid": "pickup-1",
                            "type": "PICKUP",
                            "state": "RESERVED",
                        }
                    ],
                }
            }
        assert method == "PUT"
        assert payload is not None
        assert payload["order"] == {
            "version": 7,
            "fulfillments": [{"uid": "pickup-1", "state": "COMPLETED"}],
            "state": "COMPLETED",
        }
        assert payload.get("idempotency_key")
        return {
            "order": {
                "id": "order-1",
                "state": "COMPLETED",
                "version": 8,
                "fulfillments": [
                    {
                        "uid": "pickup-1",
                        "type": "PICKUP",
                        "state": "COMPLETED",
                    }
                ],
            }
        }

    client = SquareClient(
        SquareSettings("secret-token", "LOCATION-1"), requester=requester
    )
    updated = client.complete_order("order-1", fulfillment_uid="pickup-1")

    assert updated["state"] == "COMPLETED"
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PUT"
    assert calls[1][1].endswith("/v2/orders/order-1")


def test_square_client_can_list_payments_by_updated_time_for_customer_history() -> None:
    calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def requester(method, url, headers, payload, timeout):
        calls.append((method, url, payload))
        return {"payments": []}

    client = SquareClient(
        SquareSettings(access_token="token", location_id="LOCATION-1"),
        requester=requester,
    )
    client.list_payments(
        location_id="LOCATION-1",
        updated_at_begin_time="2026-08-02T12:00:00Z",
        updated_at_end_time="2026-08-02T13:00:00Z",
    )

    assert calls[0][0] == "GET"
    assert "updated_at_begin_time=2026-08-02T12%3A00%3A00Z" in calls[0][1]
    assert "updated_at_end_time=2026-08-02T13%3A00%3A00Z" in calls[0][1]
    assert "sort_field=UPDATED_AT" in calls[0][1]
