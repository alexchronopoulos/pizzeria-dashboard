from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .domain import Item, Modifier, Order
from .square_api import SquareAPIError, SquareClient


@dataclass(frozen=True, slots=True)
class ClassificationRules:
    pizza_category_names: tuple[str, ...] = ("pizza", "pizzas")
    hidden_category_names: tuple[str, ...] = (
        "drink",
        "drinks",
        "beverage",
        "beverages",
    )
    pizza_item_keywords: tuple[str, ...] = ("pizza", "pie")
    slice_category_names: tuple[str, ...] = ("slice", "slices")
    slice_item_keywords: tuple[str, ...] = ("slice", "slices")
    hidden_item_keywords: tuple[str, ...] = (
        "drink",
        "beverage",
        "coke",
        "soda",
        "water",
    )
    salad_modifier_keywords: tuple[str, ...] = ("salad",)
    side_modifier_keywords: tuple[str, ...] = ("side",)
    cookie_modifier_keywords: tuple[str, ...] = ("cookie",)
    cookie_category_names: tuple[str, ...] = ("cookie", "cookies", "dessert", "desserts")
    cookie_item_keywords: tuple[str, ...] = ("cookie",)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ClassificationRules":
        return cls(
            pizza_category_names=_csv(
                values.get("SQUARE_PIZZA_CATEGORY_NAMES"), ("Pizza", "Pizzas")
            ),
            hidden_category_names=_csv(
                values.get("SQUARE_HIDDEN_CATEGORY_NAMES"),
                ("Drink", "Drinks", "Beverage", "Beverages"),
            ),
            pizza_item_keywords=_csv(
                values.get("SQUARE_PIZZA_ITEM_KEYWORDS"), ("pizza", "pie")
            ),
            slice_category_names=_csv(
                values.get("SQUARE_SLICE_CATEGORY_NAMES"), ("Slice", "Slices")
            ),
            slice_item_keywords=_csv(
                values.get("SQUARE_SLICE_ITEM_KEYWORDS"), ("slice", "slices")
            ),
            hidden_item_keywords=_csv(
                values.get("SQUARE_HIDDEN_ITEM_KEYWORDS"),
                ("drink", "beverage", "coke", "soda", "water"),
            ),
            salad_modifier_keywords=_csv(
                values.get("SQUARE_SALAD_MODIFIER_KEYWORDS"), ("salad",)
            ),
            side_modifier_keywords=_csv(
                values.get("SQUARE_SIDE_MODIFIER_KEYWORDS"), ("side",)
            ),
            cookie_modifier_keywords=_csv(
                values.get("SQUARE_COOKIE_MODIFIER_KEYWORDS"), ("cookie",)
            ),
            cookie_category_names=_csv(
                values.get("SQUARE_COOKIE_CATEGORY_NAMES"),
                ("Cookie", "Cookies", "Dessert", "Desserts"),
            ),
            cookie_item_keywords=_csv(
                values.get("SQUARE_COOKIE_ITEM_KEYWORDS"), ("cookie",)
            ),
        )

    def classify_item(self, name: str, category_names: Sequence[str]) -> str:
        normalized_categories = tuple(_normalize(value) for value in category_names)
        hidden_categories = tuple(_normalize(value) for value in self.hidden_category_names)
        pizza_categories = tuple(_normalize(value) for value in self.pizza_category_names)
        slice_categories = tuple(_normalize(value) for value in self.slice_category_names)
        cookie_categories = tuple(_normalize(value) for value in self.cookie_category_names)
        normalized_name = _normalize(name)

        if _contains_configured_value(normalized_categories, hidden_categories):
            return "drink"
        # POS slice items can share reporting categories with whole pies. Check
        # both the explicit slice category and the item name before pizza
        # categories so mixed pie-and-slice orders keep the pie but suppress the
        # slice from the production dashboard.
        if _contains_configured_value(normalized_categories, slice_categories):
            return "slice"
        if _matches_configured_keyword(normalized_name, self.slice_item_keywords):
            return "slice"
        if _contains_configured_value(normalized_categories, pizza_categories):
            return "pizza"
        if _contains_configured_value(normalized_categories, cookie_categories):
            return "cookie"

        # Category names frequently include descriptive words, such as
        # "Traditional Pies" or "Seasonal Special Pies". Treat the configured
        # item keywords as category keywords too, so spaces and prefixes do not
        # prevent otherwise obvious pizza categories from being recognized.
        if any(
            _normalize(keyword) in category_name
            for category_name in normalized_categories
            for keyword in self.hidden_item_keywords
        ):
            return "drink"
        if any(
            _normalize(keyword) in category_name
            for category_name in normalized_categories
            for keyword in self.slice_item_keywords
        ):
            return "slice"
        if any(
            _normalize(keyword) in category_name
            for category_name in normalized_categories
            for keyword in self.pizza_item_keywords
        ):
            return "pizza"
        if any(
            _normalize(keyword) in category_name
            for category_name in normalized_categories
            for keyword in self.cookie_item_keywords
        ):
            return "cookie"

        if any(_normalize(keyword) in normalized_name for keyword in self.hidden_item_keywords):
            return "drink"
        if any(_normalize(keyword) in normalized_name for keyword in self.pizza_item_keywords):
            return "pizza"
        if any(_normalize(keyword) in normalized_name for keyword in self.cookie_item_keywords):
            return "cookie"
        return "other"

    def classify_modifier(self, name: str) -> str:
        normalized_name = _normalize(name)
        if any(
            _normalize(keyword) in normalized_name
            for keyword in self.salad_modifier_keywords
        ):
            return "salad"
        if any(
            _normalize(keyword) in normalized_name
            for keyword in self.cookie_modifier_keywords
        ):
            return "cookie"
        if any(
            re.search(rf"\b{re.escape(_normalize(keyword))}\b", normalized_name)
            for keyword in self.side_modifier_keywords
            if _normalize(keyword)
        ):
            return "side"
        return "topping"


@dataclass(frozen=True, slots=True)
class CatalogItemInfo:
    catalog_object_id: str
    item_name: str | None = None
    variation_name: str | None = None
    category_names: tuple[str, ...] = ()
    kitchen_name: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogModifierInfo:
    catalog_object_id: str
    name: str | None = None
    kitchen_name: str | None = None


@dataclass(frozen=True, slots=True)
class SquareOrderPull:
    orders: tuple[Order, ...]
    candidates_scanned: int
    warnings: tuple[str, ...] = ()


def service_date_search_range(
    service_date: date,
    timezone_name: str,
    lookback_days: int,
) -> tuple[str, str]:
    timezone = ZoneInfo(timezone_name)
    local_start = datetime.combine(
        service_date - timedelta(days=max(lookback_days, 1)),
        time.min,
        tzinfo=timezone,
    )
    local_end = datetime.combine(
        service_date + timedelta(days=1),
        time.min,
        tzinfo=timezone,
    )
    return _rfc3339_utc(local_start), _rfc3339_utc(local_end)


def pull_square_orders_for_date(
    client: SquareClient,
    *,
    service_date: date,
    timezone_name: str,
    location_id: str,
    lookback_days: int,
    rules: ClassificationRules,
) -> SquareOrderPull:
    start_at, end_at = service_date_search_range(
        service_date, timezone_name, lookback_days
    )
    search_orders = getattr(client, "search_orders_for_service_date", None)
    if not callable(search_orders):
        search_orders = client.search_pickup_orders
    raw_orders = search_orders(
        location_id=location_id,
        created_start_at=start_at,
        created_end_at=end_at,
    )

    warnings: list[str] = []
    # SearchOrders can omit ticket_name for completed counter orders even though
    # RetrieveOrder returns it. Enrich only likely walk-ins that are missing the
    # field so Ticket Names such as "Sam 7:45" can drive automatic placement.
    retrieve_order = getattr(client, "retrieve_order", None)
    if callable(retrieve_order):
        enriched_orders: list[Mapping[str, object]] = []
        for raw_order in raw_orders:
            state = str(raw_order.get("state", "")).upper()
            has_ticket_name = bool(str(raw_order.get("ticket_name", "")).strip())
            fulfillments = _mapping_list(raw_order.get("fulfillments"))
            has_pickup_at = any(
                isinstance(f.get("pickup_details"), Mapping)
                and bool(str(f["pickup_details"].get("pickup_at", "")).strip())
                for f in fulfillments
            )
            if state == "COMPLETED" and not has_ticket_name and not has_pickup_at:
                order_id = _optional_string(raw_order.get("id"))
                if order_id:
                    try:
                        raw_order = retrieve_order(order_id)
                    except SquareAPIError as exc:
                        warnings.append(
                            f"Could not retrieve full walk-in order {order_id}; "
                            f"Ticket Name parsing may be unavailable. Square said: {exc}"
                        )
            enriched_orders.append(raw_order)
        raw_orders = tuple(enriched_orders)

    try:
        catalog_index = build_catalog_index(client, raw_orders)
        modifier_index = build_modifier_index(client, raw_orders)
    except SquareAPIError as exc:
        catalog_index = {}
        modifier_index = {}
        warnings.append(
            "Catalog details could not be loaded, so item names were used to "
            f"classify pizzas and drinks. Square said: {exc}"
        )

    receipt_numbers_by_order_id: dict[str, str] = {}
    paid_order_ids: set[str] = set()
    payment_products_by_order_id: dict[str, str] = {}
    list_payments = getattr(client, "list_payments", None)
    if callable(list_payments):
        try:
            payments = list_payments(
                location_id=location_id,
                begin_time=start_at,
                end_time=end_at,
            )
            receipt_numbers_by_order_id = build_receipt_number_index(payments)
            paid_order_ids = build_paid_order_id_set(payments)
            payment_products_by_order_id = build_payment_product_index(payments)
        except SquareAPIError as exc:
            warnings.append(
                "Payment details could not be loaded. Orders were synced without "
                f"receipt or payment-source metadata. Square said: {exc}"
            )

    orders = convert_square_orders(
        raw_orders,
        service_date=service_date,
        timezone_name=timezone_name,
        catalog_index=catalog_index,
        modifier_index=modifier_index,
        receipt_numbers_by_order_id=receipt_numbers_by_order_id,
        paid_order_ids=paid_order_ids,
        payment_products_by_order_id=payment_products_by_order_id,
        rules=rules,
    )
    return SquareOrderPull(
        orders=orders,
        candidates_scanned=len(raw_orders),
        warnings=tuple(warnings),
    )


def build_receipt_number_index(
    payments: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    """Map both Square order IDs and payment IDs to receipt numbers."""
    index: dict[str, str] = {}
    for payment in payments:
        receipt_number = _optional_string(payment.get("receipt_number"))
        if not receipt_number:
            continue
        for value in (payment.get("order_id"), payment.get("id")):
            key = _optional_string(value)
            if key and key not in index:
                index[key] = receipt_number
    return index


def build_paid_order_id_set(
    payments: Iterable[Mapping[str, object]],
) -> set[str]:
    """Return order IDs with a successful or approved Square payment."""
    paid: set[str] = set()
    for payment in payments:
        status = str(payment.get("status", "COMPLETED")).upper()
        if status not in {"APPROVED", "COMPLETED"}:
            continue
        order_id = _optional_string(payment.get("order_id"))
        if order_id:
            paid.add(order_id)
    return paid


def build_payment_product_index(
    payments: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    """Map order IDs to the Square product that accepted their payment."""
    index: dict[str, str] = {}
    for payment in payments:
        order_id = _optional_string(payment.get("order_id"))
        application_details = payment.get("application_details")
        if not order_id or not isinstance(application_details, Mapping):
            continue
        product = _optional_string(application_details.get("square_product"))
        if product and order_id not in index:
            index[order_id] = product.upper()
    return index


def _receipt_number_for_order(
    raw_order: Mapping[str, object],
    square_order_id: str,
    receipt_index: Mapping[str, str],
) -> str | None:
    direct = receipt_index.get(square_order_id)
    if direct:
        return direct
    for tender in _mapping_list(raw_order.get("tenders")):
        for value in (tender.get("payment_id"), tender.get("id")):
            payment_id = _optional_string(value)
            if payment_id and payment_id in receipt_index:
                return receipt_index[payment_id]
    return None


def build_catalog_index(
    client: SquareClient,
    raw_orders: Iterable[Mapping[str, object]],
) -> dict[str, CatalogItemInfo]:
    variation_ids = tuple(
        dict.fromkeys(
            catalog_id
            for raw_order in raw_orders
            for line_item in _mapping_list(raw_order.get("line_items"))
            if (catalog_id := _optional_string(line_item.get("catalog_object_id")))
        )
    )
    if not variation_ids:
        return {}

    first_pass = client.batch_retrieve_catalog_objects(
        variation_ids, include_related_objects=True
    )
    objects_by_id = {
        str(value.get("id")): value
        for value in first_pass
        if value.get("id") is not None
    }

    parent_item_ids: set[str] = set()
    for variation_id in variation_ids:
        value = objects_by_id.get(variation_id)
        if not value:
            continue
        if value.get("type") == "ITEM_VARIATION":
            data = value.get("item_variation_data")
            if isinstance(data, Mapping) and data.get("item_id"):
                parent_item_ids.add(str(data["item_id"]))
        elif value.get("type") == "ITEM":
            parent_item_ids.add(variation_id)

    missing_parent_ids = tuple(
        parent_id for parent_id in parent_item_ids if parent_id not in objects_by_id
    )
    if missing_parent_ids:
        for value in client.batch_retrieve_catalog_objects(missing_parent_ids):
            if value.get("id") is not None:
                objects_by_id[str(value["id"])] = value

    category_ids: set[str] = set()
    for parent_id in parent_item_ids:
        parent = objects_by_id.get(parent_id)
        if not parent:
            continue
        item_data = parent.get("item_data")
        if not isinstance(item_data, Mapping):
            continue
        for category in _mapping_list(item_data.get("categories")):
            if category.get("id"):
                category_ids.add(str(category["id"]))
        if item_data.get("category_id"):
            category_ids.add(str(item_data["category_id"]))
        reporting_category = item_data.get("reporting_category")
        if isinstance(reporting_category, Mapping) and reporting_category.get("id"):
            category_ids.add(str(reporting_category["id"]))

    category_names: dict[str, str] = {}
    if category_ids:
        for value in client.batch_retrieve_catalog_objects(tuple(category_ids)):
            if value.get("type") != "CATEGORY" or value.get("id") is None:
                continue
            data = value.get("category_data")
            if isinstance(data, Mapping) and data.get("name"):
                category_names[str(value["id"])] = str(data["name"])

    index: dict[str, CatalogItemInfo] = {}
    for variation_id in variation_ids:
        value = objects_by_id.get(variation_id)
        if not value:
            continue

        parent: Mapping[str, object] | None = None
        variation_name: str | None = None
        if value.get("type") == "ITEM_VARIATION":
            variation_data = value.get("item_variation_data")
            if isinstance(variation_data, Mapping):
                variation_name = _optional_string(variation_data.get("name"))
                parent_id = _optional_string(variation_data.get("item_id"))
                parent = objects_by_id.get(parent_id or "")
        elif value.get("type") == "ITEM":
            parent = value

        item_name: str | None = None
        item_categories: list[str] = []
        if parent:
            item_data = parent.get("item_data")
            if isinstance(item_data, Mapping):
                item_name = _optional_string(item_data.get("name"))
                kitchen_name = _optional_string(item_data.get("kitchen_name"))
                for category in _mapping_list(item_data.get("categories")):
                    category_id = _optional_string(category.get("id"))
                    if category_id and category_id in category_names:
                        item_categories.append(category_names[category_id])
                legacy_category_id = _optional_string(item_data.get("category_id"))
                if (
                    legacy_category_id
                    and legacy_category_id in category_names
                    and category_names[legacy_category_id] not in item_categories
                ):
                    item_categories.append(category_names[legacy_category_id])

                reporting_category = item_data.get("reporting_category")
                if isinstance(reporting_category, Mapping):
                    reporting_category_id = _optional_string(
                        reporting_category.get("id")
                    )
                    if (
                        reporting_category_id
                        and reporting_category_id in category_names
                        and category_names[reporting_category_id]
                        not in item_categories
                    ):
                        item_categories.append(category_names[reporting_category_id])
            else:
                kitchen_name = None
        else:
            kitchen_name = None

        index[variation_id] = CatalogItemInfo(
            catalog_object_id=variation_id,
            item_name=item_name,
            kitchen_name=kitchen_name,
            variation_name=variation_name,
            category_names=tuple(item_categories),
        )
    return index


def build_modifier_index(
    client: SquareClient,
    raw_orders: Iterable[Mapping[str, object]],
) -> dict[str, CatalogModifierInfo]:
    modifier_ids = tuple(
        dict.fromkeys(
            catalog_id
            for raw_order in raw_orders
            for line_item in _mapping_list(raw_order.get("line_items"))
            for modifier in _mapping_list(line_item.get("modifiers"))
            if (catalog_id := _optional_string(modifier.get("catalog_object_id")))
        )
    )
    if not modifier_ids:
        return {}

    index: dict[str, CatalogModifierInfo] = {}
    for value in client.batch_retrieve_catalog_objects(modifier_ids):
        if value.get("type") != "MODIFIER" or value.get("id") is None:
            continue
        modifier_data = value.get("modifier_data")
        if not isinstance(modifier_data, Mapping):
            continue
        catalog_object_id = str(value["id"])
        index[catalog_object_id] = CatalogModifierInfo(
            catalog_object_id=catalog_object_id,
            name=_optional_string(modifier_data.get("name")),
            kitchen_name=_optional_string(modifier_data.get("kitchen_name")),
        )
    return index


def convert_square_orders(
    raw_orders: Iterable[Mapping[str, object]],
    *,
    service_date: date,
    timezone_name: str,
    catalog_index: Mapping[str, CatalogItemInfo],
    modifier_index: Mapping[str, CatalogModifierInfo] | None = None,
    receipt_numbers_by_order_id: Mapping[str, str] | None = None,
    paid_order_ids: set[str] | None = None,
    payment_products_by_order_id: Mapping[str, str] | None = None,
    rules: ClassificationRules,
) -> tuple[Order, ...]:
    timezone = ZoneInfo(timezone_name)
    modifier_index = modifier_index or {}
    receipt_numbers_by_order_id = receipt_numbers_by_order_id or {}
    paid_order_ids = paid_order_ids or set()
    payment_products_by_order_id = payment_products_by_order_id or {}
    converted: list[Order] = []

    for raw_order in raw_orders:
        order_state = str(raw_order.get("state", "")).upper()
        if order_state not in {"OPEN", "COMPLETED"}:
            continue
        square_order_id = _optional_string(raw_order.get("id"))
        if not square_order_id:
            continue

        raw_fulfillments = _mapping_list(raw_order.get("fulfillments"))
        matching_fulfillments: list[tuple[Mapping[str, object], datetime]] = []
        has_pickup_time = False
        has_non_pickup_fulfillment = False
        for fulfillment in raw_fulfillments:
            if str(fulfillment.get("type", "")) != "PICKUP":
                has_non_pickup_fulfillment = True
                continue
            state = str(fulfillment.get("state", ""))
            if state in {"CANCELED", "FAILED"}:
                continue
            pickup_details = fulfillment.get("pickup_details")
            if not isinstance(pickup_details, Mapping):
                continue
            pickup_at = _parse_square_datetime(pickup_details.get("pickup_at"))
            if pickup_at is None:
                continue
            has_pickup_time = True
            local_pickup_at = pickup_at.astimezone(timezone)
            if local_pickup_at.date() == service_date:
                matching_fulfillments.append((fulfillment, local_pickup_at))

        source_created_at = _parse_square_datetime(
            raw_order.get("created_at"), timezone=timezone
        )
        source_closed_at = _parse_square_datetime(
            raw_order.get("closed_at"), timezone=timezone
        )
        creation_product = _creation_product(raw_order)
        ticket_name = _optional_string(raw_order.get("ticket_name"))

        for fulfillment, pickup_at in matching_fulfillments:
            uid = _optional_string(fulfillment.get("uid"))
            cache_id = (
                square_order_id
                if len(matching_fulfillments) == 1
                else f"{square_order_id}:{uid or pickup_at.isoformat()}"
            )
            items = _convert_line_items(
                raw_order,
                fulfillment,
                catalog_index=catalog_index,
                modifier_index=modifier_index,
                rules=rules,
            )
            pickup_details = fulfillment.get("pickup_details")
            recipient = (
                pickup_details.get("recipient")
                if isinstance(pickup_details, Mapping)
                else None
            )
            customer_name = "Guest"
            if isinstance(recipient, Mapping) and recipient.get("display_name"):
                customer_name = str(recipient["display_name"])

            fulfillment_state = _optional_string(fulfillment.get("state"))
            # Square can return completed pickup records created by POS or other
            # workflows that have no recipient name. They are not useful on the
            # production board and otherwise appear as mysterious "Guest" orders.
            # Keep active nameless orders, and keep named completed orders because
            # the dashboard intentionally uses completed fulfillments to represent
            # capacity that was manually released.
            if customer_name == "Guest" and fulfillment_state == "COMPLETED":
                continue
            converted.append(
                Order(
                    order_id=cache_id,
                    customer_name=customer_name,
                    pickup_at=pickup_at,
                    items=items,
                    receipt_number=_receipt_number_for_order(
                        raw_order, square_order_id, receipt_numbers_by_order_id
                    ),
                    released=fulfillment_state == "COMPLETED",
                    square_order_id=square_order_id,
                    square_version=_optional_int(raw_order.get("version")),
                    location_id=_optional_string(raw_order.get("location_id")),
                    fulfillment_uid=uid,
                    fulfillment_state=fulfillment_state,
                    source_updated_at=_parse_square_datetime(
                        raw_order.get("updated_at"), timezone=timezone
                    ),
                    source_created_at=source_created_at,
                    source_closed_at=source_closed_at,
                    creation_product=creation_product,
                    ticket_name=ticket_name,
                )
            )

        # Any order with a real pickup timestamp belongs to the scheduled
        # fulfillment path, even when that pickup is for another service date.
        # A completed order whose pickup fulfillment exists but has no pickup_at
        # is intentionally treated as unscheduled so it can be assigned locally.
        if matching_fulfillments or has_pickup_time:
            continue

        walk_in_at = _walk_in_event_at(
            raw_order,
            service_date=service_date,
            timezone=timezone,
        )
        receipt_number = _receipt_number_for_order(
            raw_order, square_order_id, receipt_numbers_by_order_id
        )
        if walk_in_at is None or has_non_pickup_fulfillment:
            continue

        items = _convert_line_items(
            raw_order,
            None,
            catalog_index=catalog_index,
            modifier_index=modifier_index,
            rules=rules,
        )
        converted.append(
            Order(
                order_id=square_order_id,
                customer_name=ticket_name or "Walk-in",
                pickup_at=walk_in_at,
                items=items,
                receipt_number=receipt_number,
                released=False,
                square_order_id=square_order_id,
                square_version=_optional_int(raw_order.get("version")),
                location_id=_optional_string(raw_order.get("location_id")),
                fulfillment_uid=None,
                fulfillment_state=None,
                source_updated_at=_parse_square_datetime(
                    raw_order.get("updated_at"), timezone=timezone
                ),
                is_walk_in=True,
                source_created_at=source_created_at,
                source_closed_at=source_closed_at,
                creation_product=creation_product,
                ticket_name=ticket_name,
            )
        )

    return tuple(sorted(converted, key=lambda order: (order.pickup_at, order.order_id)))


def _convert_line_items(
    raw_order: Mapping[str, object],
    fulfillment: Mapping[str, object] | None,
    *,
    catalog_index: Mapping[str, CatalogItemInfo],
    modifier_index: Mapping[str, CatalogModifierInfo],
    rules: ClassificationRules,
) -> tuple[Item, ...]:
    applicable_uids = (
        _fulfillment_line_item_uids(fulfillment) if fulfillment is not None else set()
    )
    items: list[Item] = []
    for line_item in _mapping_list(raw_order.get("line_items")):
        line_item_uid = _optional_string(line_item.get("uid"))
        if applicable_uids and line_item_uid not in applicable_uids:
            continue

        catalog_object_id = _optional_string(line_item.get("catalog_object_id"))
        catalog_info = catalog_index.get(catalog_object_id or "")
        display_name = (
            (catalog_info.kitchen_name if catalog_info else None)
            or _optional_string(line_item.get("name"))
            or (catalog_info.item_name if catalog_info else None)
            or "Item"
        )
        categories = catalog_info.category_names if catalog_info else ()
        converted_modifiers: list[Modifier] = []
        for modifier in _mapping_list(line_item.get("modifiers")):
            modifier_catalog_id = _optional_string(
                modifier.get("catalog_object_id")
            )
            catalog_modifier = modifier_index.get(modifier_catalog_id or "")
            modifier_name = (
                (catalog_modifier.kitchen_name if catalog_modifier else None)
                or _optional_string(modifier.get("name"))
                or (catalog_modifier.name if catalog_modifier else None)
                or "Modifier"
            )
            converted_modifiers.append(
                Modifier(
                    name=modifier_name,
                    category=rules.classify_modifier(modifier_name),
                    quantity=_quantity(modifier.get("quantity")),
                    catalog_object_id=modifier_catalog_id,
                )
            )
        modifiers = tuple(converted_modifiers)
        items.append(
            Item(
                name=display_name,
                quantity=_quantity(line_item.get("quantity")),
                category=rules.classify_item(display_name, categories),
                modifiers=modifiers,
                catalog_object_id=catalog_object_id,
                variation_name=(catalog_info.variation_name if catalog_info else None),
                catalog_categories=categories,
            )
        )
    return tuple(items)


def _fulfillment_line_item_uids(
    fulfillment: Mapping[str, object],
) -> set[str]:
    entries = _mapping_list(fulfillment.get("entries"))
    return {
        str(entry["line_item_uid"])
        for entry in entries
        if entry.get("line_item_uid")
    }


def _creation_product(raw_order: Mapping[str, object]) -> str | None:
    creation_source = raw_order.get("creation_source")
    if isinstance(creation_source, Mapping):
        return _optional_string(creation_source.get("product"))
    return None



def _walk_in_event_at(
    raw_order: Mapping[str, object],
    *,
    service_date: date,
    timezone: ZoneInfo,
) -> datetime | None:
    """Return the local event time for an unscheduled completed order.

    Counter orders are not required to expose a useful source label, payment
    association, receipt number, or pickup fulfillment. The operational signal
    is simply that the order is completed, has no actual pickup timestamp, and
    was either created or closed on the selected local service date.

    Prefer ``closed_at`` for display because it is closest to the payment time.
    When it belongs to another day, still accept ``created_at`` if that timestamp
    falls on the selected service date.
    """
    if str(raw_order.get("state", "")).upper() != "COMPLETED":
        return None

    local_closed_at = _parse_square_datetime(
        raw_order.get("closed_at"), timezone=timezone
    )
    if local_closed_at is not None and local_closed_at.date() == service_date:
        return local_closed_at

    local_created_at = _parse_square_datetime(
        raw_order.get("created_at"), timezone=timezone
    )
    if local_created_at is not None and local_created_at.date() == service_date:
        return local_created_at

    return None


def _matches_configured_keyword(value: str, keywords: Sequence[str]) -> bool:
    """Return whether a normalized value contains a configured whole phrase."""
    return any(
        bool(re.search(rf"\b{re.escape(normalized)}\b", value))
        for keyword in keywords
        if (normalized := _normalize(keyword))
    )


def _csv(value: object, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, (tuple, list)):
        result = tuple(str(item).strip() for item in value if str(item).strip())
        return result or tuple(default)
    result = tuple(part.strip() for part in str(value).split(",") if part.strip())
    return result or tuple(default)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _contains_configured_value(
    actual_values: Sequence[str], configured_values: Sequence[str]
) -> bool:
    for actual in actual_values:
        for configured in configured_values:
            if configured and (actual == configured or configured in actual):
                return True
    return False


def _mapping_list(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _quantity(value: object) -> int:
    try:
        quantity = Decimal(str(value or "1"))
    except InvalidOperation:
        return 1
    if quantity <= 0:
        return 1
    return max(int(quantity), 1)


def _parse_square_datetime(
    value: object, *, timezone: ZoneInfo | None = None
) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if timezone is not None:
        return parsed.astimezone(timezone)
    return parsed


def _rfc3339_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
