from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .customer_history import (
    CustomerHistory,
    CustomerHistoryOrder,
    CustomerHistorySyncInfo,
    CustomerSummary,
)
from .domain import Item, Modifier, Order, order_from_payload, order_to_payload


SCHEMA_VERSION = 5

_UNSCHEDULED_ASSIGNMENT = "__UNSCHEDULED__"


@dataclass(frozen=True, slots=True)
class SyncInfo:
    service_date: date
    source: str
    synced_at: datetime
    order_count: int


@dataclass(frozen=True, slots=True)
class MergeResult:
    info: SyncInfo
    changed_count: int
    removed_count: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT NOT NULL,
                service_date TEXT NOT NULL,
                pickup_at TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                released INTEGER NOT NULL DEFAULT 0 CHECK (released IN (0, 1)),
                source_payload_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                source_updated_at TEXT,
                cached_at TEXT NOT NULL,
                PRIMARY KEY (order_id, service_date)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_service_pickup
                ON orders (service_date, pickup_at);

            CREATE TABLE IF NOT EXISTS service_states (
                service_date TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                service_date TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                order_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_slot_assignments (
                service_date TEXT NOT NULL,
                order_id TEXT NOT NULL,
                pickup_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (service_date, order_id)
            );

            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_history_orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                ordered_at TEXT NOT NULL,
                service_date TEXT NOT NULL,
                source TEXT,
                items_json TEXT NOT NULL,
                cached_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_customer_history_customer_ordered
                ON customer_history_orders (customer_id, ordered_at DESC);

            CREATE TABLE IF NOT EXISTS customer_history_sync (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                synced_at TEXT NOT NULL,
                start_at TEXT NOT NULL,
                payment_count INTEGER NOT NULL,
                order_count INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO app_metadata (key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )


def replace_orders_for_date(
    path: Path,
    service_date: date,
    orders: Iterable[Order],
    *,
    source: str,
    synced_at: datetime | None = None,
) -> SyncInfo:
    """Atomically replace the cached source snapshot for one service date."""
    order_list = tuple(orders)
    sync_time = synced_at or _utc_now()
    date_key = service_date.isoformat()

    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for order in order_list:
            payload = json.dumps(
                order_to_payload(order),
                separators=(",", ":"),
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO orders (
                    order_id,
                    service_date,
                    pickup_at,
                    customer_name,
                    released,
                    source_payload_json,
                    source_updated_at,
                    cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id, service_date) DO UPDATE SET
                    pickup_at = excluded.pickup_at,
                    customer_name = excluded.customer_name,
                    released = excluded.released,
                    source_payload_json = excluded.source_payload_json,
                    source_updated_at = excluded.source_updated_at,
                    cached_at = excluded.cached_at
                """,
                (
                    order.order_id,
                    date_key,
                    order.pickup_at.isoformat(),
                    order.customer_name,
                    int(order.released),
                    payload,
                    (
                        order.source_updated_at.isoformat()
                        if order.source_updated_at
                        else None
                    ),
                    sync_time.isoformat(),
                ),
            )

        if order_list:
            placeholders = ",".join("?" for _ in order_list)
            connection.execute(
                f"""
                DELETE FROM orders
                WHERE service_date = ?
                  AND order_id NOT IN ({placeholders})
                """,
                (date_key, *(order.order_id for order in order_list)),
            )
            connection.execute(
                f"""
                DELETE FROM order_slot_assignments
                WHERE service_date = ?
                  AND order_id NOT IN ({placeholders})
                """,
                (date_key, *(order.order_id for order in order_list)),
            )
        else:
            connection.execute(
                "DELETE FROM orders WHERE service_date = ?",
                (date_key,),
            )
            connection.execute(
                "DELETE FROM order_slot_assignments WHERE service_date = ?",
                (date_key,),
            )

        connection.execute(
            """
            INSERT INTO sync_runs (service_date, source, synced_at, order_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(service_date) DO UPDATE SET
                source = excluded.source,
                synced_at = excluded.synced_at,
                order_count = excluded.order_count
            """,
            (date_key, source, sync_time.isoformat(), len(order_list)),
        )

    return SyncInfo(service_date, source, sync_time, len(order_list))


def merge_orders_for_date(
    path: Path,
    service_date: date,
    orders: Iterable[Order],
    *,
    candidate_square_order_ids: Iterable[str],
    source: str,
    synced_at: datetime | None = None,
) -> MergeResult:
    """Merge an incremental Square refresh into one cached service date.

    Every raw Square order returned by the UPDATED_AT search is represented in
    ``candidate_square_order_ids``. If one of those candidates no longer
    converts into a production order (for example, it was canceled or its
    pickup date changed), its stale cached rows are removed. Orders that were
    not part of the narrow refresh window remain untouched.
    """
    order_list = tuple(orders)
    candidate_ids = {value for value in candidate_square_order_ids if value}
    sync_time = synced_at or _utc_now()
    date_key = service_date.isoformat()
    serialized_orders: dict[str, str] = {
        order.order_id: json.dumps(
            order_to_payload(order), separators=(",", ":"), sort_keys=True
        )
        for order in order_list
    }
    eligible_cache_ids = set(serialized_orders)
    changed_count = 0
    removed_count = 0

    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing_rows = connection.execute(
            """
            SELECT order_id, source_payload_json
            FROM orders
            WHERE service_date = ?
            """,
            (date_key,),
        ).fetchall()
        existing_payloads = {
            str(row["order_id"]): str(row["source_payload_json"])
            for row in existing_rows
        }

        stale_cache_ids: list[str] = []
        if candidate_ids:
            for row in existing_rows:
                cache_id = str(row["order_id"])
                if cache_id in eligible_cache_ids:
                    continue
                try:
                    payload = json.loads(str(row["source_payload_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                square_order_id = payload.get("square_order_id")
                if square_order_id and str(square_order_id) in candidate_ids:
                    stale_cache_ids.append(cache_id)

        for order in order_list:
            payload = serialized_orders[order.order_id]
            if existing_payloads.get(order.order_id) != payload:
                changed_count += 1
            connection.execute(
                """
                INSERT INTO orders (
                    order_id,
                    service_date,
                    pickup_at,
                    customer_name,
                    released,
                    source_payload_json,
                    source_updated_at,
                    cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id, service_date) DO UPDATE SET
                    pickup_at = excluded.pickup_at,
                    customer_name = excluded.customer_name,
                    released = excluded.released,
                    source_payload_json = excluded.source_payload_json,
                    source_updated_at = excluded.source_updated_at,
                    cached_at = excluded.cached_at
                """,
                (
                    order.order_id,
                    date_key,
                    order.pickup_at.isoformat(),
                    order.customer_name,
                    int(order.released),
                    payload,
                    (
                        order.source_updated_at.isoformat()
                        if order.source_updated_at
                        else None
                    ),
                    sync_time.isoformat(),
                ),
            )

        if stale_cache_ids:
            placeholders = ",".join("?" for _ in stale_cache_ids)
            connection.execute(
                f"DELETE FROM orders WHERE service_date = ? AND order_id IN ({placeholders})",
                (date_key, *stale_cache_ids),
            )
            connection.execute(
                f"DELETE FROM order_slot_assignments WHERE service_date = ? AND order_id IN ({placeholders})",
                (date_key, *stale_cache_ids),
            )
            removed_count = len(stale_cache_ids)

        row = connection.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE service_date = ?",
            (date_key,),
        ).fetchone()
        order_count = int(row["total"]) if row is not None else 0
        connection.execute(
            """
            INSERT INTO sync_runs (service_date, source, synced_at, order_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(service_date) DO UPDATE SET
                source = excluded.source,
                synced_at = excluded.synced_at,
                order_count = excluded.order_count
            """,
            (date_key, source, sync_time.isoformat(), order_count),
        )

    return MergeResult(
        info=SyncInfo(service_date, source, sync_time, order_count),
        changed_count=changed_count,
        removed_count=removed_count,
    )


def load_orders_for_date(path: Path, service_date: date) -> tuple[Order, ...]:
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT source_payload_json
            FROM orders
            WHERE service_date = ?
            ORDER BY pickup_at, order_id
            """,
            (service_date.isoformat(),),
        ).fetchall()

    orders: list[Order] = []
    for row in rows:
        try:
            payload = json.loads(row["source_payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            orders.append(order_from_payload(payload))
    return tuple(orders)


def load_order_for_date(
    path: Path, service_date: date, order_id: str
) -> Order | None:
    """Load one cached order document for the details view."""
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT source_payload_json
            FROM orders
            WHERE service_date = ? AND order_id = ?
            """,
            (service_date.isoformat(), order_id),
        ).fetchone()

    if row is None:
        return None
    try:
        payload = json.loads(row["source_payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return order_from_payload(payload)


def has_orders_for_date(path: Path, service_date: date) -> bool:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM orders WHERE service_date = ? LIMIT 1",
            (service_date.isoformat(),),
        ).fetchone()
    return row is not None


def load_sync_info(path: Path, service_date: date) -> SyncInfo | None:
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT source, synced_at, order_count
            FROM sync_runs
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchone()
    if row is None:
        return None
    return SyncInfo(
        service_date=service_date,
        source=str(row["source"]),
        synced_at=datetime.fromisoformat(str(row["synced_at"])),
        order_count=int(row["order_count"]),
    )


def load_order_slot_assignment_overrides(
    path: Path, service_date: date
) -> dict[str, datetime | None]:
    """Load explicit walk-in slot choices, including forced-unscheduled rows."""
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT order_id, pickup_at
            FROM order_slot_assignments
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchall()

    assignments: dict[str, datetime | None] = {}
    for row in rows:
        order_id = str(row["order_id"])
        raw_pickup_at = str(row["pickup_at"])
        if raw_pickup_at == _UNSCHEDULED_ASSIGNMENT:
            assignments[order_id] = None
            continue
        try:
            assignments[order_id] = datetime.fromisoformat(raw_pickup_at)
        except ValueError:
            continue
    return assignments


def load_order_slot_assignments(
    path: Path, service_date: date
) -> dict[str, datetime]:
    """Load only walk-ins assigned to actual service slots.

    Kept as the public compatibility view used by existing callers and tests.
    Explicit unscheduled overrides are available through
    :func:`load_order_slot_assignment_overrides`.
    """
    return {
        order_id: pickup_at
        for order_id, pickup_at in load_order_slot_assignment_overrides(
            path, service_date
        ).items()
        if pickup_at is not None
    }


def save_order_slot_assignment(
    path: Path,
    service_date: date,
    order_id: str,
    pickup_at: datetime | None,
) -> None:
    with _connect(path) as connection:
        serialized_pickup_at = (
            pickup_at.isoformat() if pickup_at is not None else _UNSCHEDULED_ASSIGNMENT
        )
        connection.execute(
            """
            INSERT INTO order_slot_assignments (
                service_date, order_id, pickup_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(service_date, order_id) DO UPDATE SET
                pickup_at = excluded.pickup_at,
                updated_at = excluded.updated_at
            """,
            (
                service_date.isoformat(),
                order_id,
                serialized_pickup_at,
                _utc_now().isoformat(),
            ),
        )


def load_service_state_payload(path: Path, service_date: date) -> dict[str, object] | None:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT state_json FROM service_states WHERE service_date = ?",
            (service_date.isoformat(),),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["state_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_service_state_payload(
    path: Path,
    service_date: date,
    payload: Mapping[str, object],
) -> None:
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO service_states (service_date, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(service_date) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (service_date.isoformat(), serialized, _utc_now().isoformat()),
        )


def _metadata_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else str(row["value"])


def migrate_legacy_service_state(path: Path, legacy_json_path: Path) -> int:
    """Import the previous JSON prep-state file once, without overwriting SQLite."""
    migration_key = "legacy_service_state_json_migrated"
    with _connect(path) as connection:
        if _metadata_value(connection, migration_key) == "1":
            return 0

        imported = 0
        if not legacy_json_path.exists():
            return 0
        try:
            raw = json.loads(legacy_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0

        services: dict[str, object] = {}
        if isinstance(raw, dict) and isinstance(raw.get("services"), dict):
            services = raw["services"]
        elif isinstance(raw, dict) and (
            "dough_balls_prepared" in raw or "salad_prepared" in raw
        ):
            # A legacy undated state cannot be assigned safely. Keep the file in
            # place and let the operator save it for a selected date if needed.
            services = {}

        for service_date, state in services.items():
            if not isinstance(service_date, str) or not isinstance(state, dict):
                continue
            cursor = connection.execute(
                """
                INSERT INTO service_states (service_date, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service_date) DO NOTHING
                """,
                (
                    service_date,
                    json.dumps(state, separators=(",", ":"), sort_keys=True),
                    _utc_now().isoformat(),
                ),
            )
            imported += max(cursor.rowcount, 0)

        connection.execute(
            """
            INSERT INTO app_metadata (key, value)
            VALUES (?, '1')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (migration_key,),
        )
    return imported


def load_app_metadata(path: Path, key: str) -> str | None:
    with _connect(path) as connection:
        return _metadata_value(connection, key)


def save_app_metadata(path: Path, key: str, value: str) -> None:
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO app_metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def _history_items_to_json(items: Iterable[Item]) -> str:
    return json.dumps(
        [
            {
                "name": item.name,
                "quantity": item.quantity,
                "category": item.category,
                "catalog_object_id": item.catalog_object_id,
                "variation_name": item.variation_name,
                "catalog_categories": list(item.catalog_categories),
                "modifiers": [
                    {
                        "name": modifier.name,
                        "category": modifier.category,
                        "quantity": modifier.quantity,
                        "catalog_object_id": modifier.catalog_object_id,
                    }
                    for modifier in item.modifiers
                ],
            }
            for item in items
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


def _history_items_from_json(value: str) -> tuple[Item, ...]:
    try:
        raw_items = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw_items, list):
        return ()

    items: list[Item] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        modifiers: list[Modifier] = []
        raw_modifiers = raw_item.get("modifiers", [])
        if isinstance(raw_modifiers, list):
            for raw_modifier in raw_modifiers:
                if not isinstance(raw_modifier, Mapping):
                    continue
                try:
                    quantity = max(int(raw_modifier.get("quantity", 1)), 1)
                except (TypeError, ValueError):
                    quantity = 1
                modifiers.append(
                    Modifier(
                        name=str(raw_modifier.get("name", "Modifier")),
                        category=str(raw_modifier.get("category", "topping")),
                        quantity=quantity,
                        catalog_object_id=(
                            str(raw_modifier["catalog_object_id"])
                            if raw_modifier.get("catalog_object_id")
                            else None
                        ),
                    )
                )
        raw_categories = raw_item.get("catalog_categories", [])
        categories = (
            tuple(str(entry) for entry in raw_categories)
            if isinstance(raw_categories, list)
            else ()
        )
        try:
            quantity = max(int(raw_item.get("quantity", 1)), 1)
        except (TypeError, ValueError):
            quantity = 1
        items.append(
            Item(
                name=str(raw_item.get("name", "Item")),
                quantity=quantity,
                category=str(raw_item.get("category", "other")),
                modifiers=tuple(modifiers),
                catalog_object_id=(
                    str(raw_item["catalog_object_id"])
                    if raw_item.get("catalog_object_id")
                    else None
                ),
                variation_name=(
                    str(raw_item["variation_name"])
                    if raw_item.get("variation_name")
                    else None
                ),
                catalog_categories=categories,
            )
        )
    return tuple(items)


def replace_customer_history(
    path: Path,
    orders: Iterable[CustomerHistoryOrder],
    *,
    synced_at: datetime,
    start_at: datetime,
    payment_count: int,
) -> CustomerHistorySyncInfo:
    """Replace the rebuildable customer-history index atomically."""
    order_list = tuple(orders)
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM customer_history_orders")
        for order in order_list:
            connection.execute(
                """
                INSERT INTO customer_history_orders (
                    order_id, customer_id, ordered_at, service_date,
                    source, items_json, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.customer_id,
                    order.ordered_at.isoformat(),
                    order.service_date.isoformat(),
                    order.source,
                    _history_items_to_json(order.items),
                    synced_at.isoformat(),
                ),
            )
        connection.execute(
            """
            INSERT INTO customer_history_sync (
                id, synced_at, start_at, payment_count, order_count
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                synced_at = excluded.synced_at,
                start_at = excluded.start_at,
                payment_count = excluded.payment_count,
                order_count = excluded.order_count
            """,
            (
                synced_at.isoformat(),
                start_at.isoformat(),
                payment_count,
                len(order_list),
            ),
        )
    return CustomerHistorySyncInfo(
        synced_at=synced_at,
        start_at=start_at,
        payment_count=payment_count,
        order_count=len(order_list),
    )


def merge_customer_history(
    path: Path,
    orders: Iterable[CustomerHistoryOrder],
    *,
    synced_at: datetime,
    start_at: datetime,
    payment_count: int,
) -> tuple[CustomerHistorySyncInfo, int]:
    """Merge a narrow payment window into the customer-history cache."""
    order_list = tuple(orders)
    changed_count = 0
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = {
            str(row["order_id"]): (
                str(row["customer_id"]),
                str(row["ordered_at"]),
                str(row["service_date"]),
                row["source"],
                str(row["items_json"]),
            )
            for row in connection.execute(
                """
                SELECT order_id, customer_id, ordered_at, service_date, source, items_json
                FROM customer_history_orders
                """
            ).fetchall()
        }
        for order in order_list:
            items_json = _history_items_to_json(order.items)
            serialized = (
                order.customer_id,
                order.ordered_at.isoformat(),
                order.service_date.isoformat(),
                order.source,
                items_json,
            )
            if existing.get(order.order_id) != serialized:
                changed_count += 1
            connection.execute(
                """
                INSERT INTO customer_history_orders (
                    order_id, customer_id, ordered_at, service_date,
                    source, items_json, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    customer_id = excluded.customer_id,
                    ordered_at = excluded.ordered_at,
                    service_date = excluded.service_date,
                    source = excluded.source,
                    items_json = excluded.items_json,
                    cached_at = excluded.cached_at
                """,
                (
                    order.order_id,
                    order.customer_id,
                    order.ordered_at.isoformat(),
                    order.service_date.isoformat(),
                    order.source,
                    items_json,
                    synced_at.isoformat(),
                ),
            )
        total_orders = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM customer_history_orders"
            ).fetchone()["total"]
        )
        previous = connection.execute(
            "SELECT start_at, payment_count FROM customer_history_sync WHERE id = 1"
        ).fetchone()
        effective_start = (
            min(datetime.fromisoformat(str(previous["start_at"])), start_at)
            if previous is not None
            else start_at
        )
        cumulative_payment_count = (
            max(int(previous["payment_count"]), payment_count)
            if previous is not None
            else payment_count
        )
        connection.execute(
            """
            INSERT INTO customer_history_sync (
                id, synced_at, start_at, payment_count, order_count
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                synced_at = excluded.synced_at,
                start_at = excluded.start_at,
                payment_count = excluded.payment_count,
                order_count = excluded.order_count
            """,
            (
                synced_at.isoformat(),
                effective_start.isoformat(),
                cumulative_payment_count,
                total_orders,
            ),
        )
    return (
        CustomerHistorySyncInfo(
            synced_at=synced_at,
            start_at=effective_start,
            payment_count=cumulative_payment_count,
            order_count=total_orders,
        ),
        changed_count,
    )


def load_customer_history_sync_info(path: Path) -> CustomerHistorySyncInfo | None:
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT synced_at, start_at, payment_count, order_count
            FROM customer_history_sync
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    return CustomerHistorySyncInfo(
        synced_at=datetime.fromisoformat(str(row["synced_at"])),
        start_at=datetime.fromisoformat(str(row["start_at"])),
        payment_count=int(row["payment_count"]),
        order_count=int(row["order_count"]),
    )


def load_customer_summaries_for_orders(
    path: Path, order_ids: Iterable[str]
) -> dict[str, CustomerSummary]:
    unique_ids = tuple(dict.fromkeys(value for value in order_ids if value))
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    with _connect(path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                current.order_id AS current_order_id,
                current.customer_id AS customer_id,
                COUNT(history.order_id) AS order_count,
                MIN(history.ordered_at) AS first_order_at,
                MAX(history.ordered_at) AS last_order_at
            FROM customer_history_orders AS current
            JOIN customer_history_orders AS history
              ON history.customer_id = current.customer_id
            WHERE current.order_id IN ({placeholders})
            GROUP BY current.order_id, current.customer_id
            """,
            unique_ids,
        ).fetchall()
    return {
        str(row["current_order_id"]): CustomerSummary(
            customer_id=str(row["customer_id"]),
            order_count=int(row["order_count"]),
            first_order_at=datetime.fromisoformat(str(row["first_order_at"])),
            last_order_at=datetime.fromisoformat(str(row["last_order_at"])),
        )
        for row in rows
    }


def load_customer_history_for_order(
    path: Path, order_id: str
) -> CustomerHistory | None:
    with _connect(path) as connection:
        current = connection.execute(
            """
            SELECT customer_id
            FROM customer_history_orders
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        if current is None:
            return None
        customer_id = str(current["customer_id"])
        rows = connection.execute(
            """
            SELECT order_id, ordered_at, service_date, source, items_json
            FROM customer_history_orders
            WHERE customer_id = ?
            ORDER BY ordered_at DESC, order_id DESC
            """,
            (customer_id,),
        ).fetchall()

    orders = tuple(
        CustomerHistoryOrder(
            customer_id=customer_id,
            order_id=str(row["order_id"]),
            ordered_at=datetime.fromisoformat(str(row["ordered_at"])),
            service_date=date.fromisoformat(str(row["service_date"])),
            source=str(row["source"]) if row["source"] is not None else None,
            items=_history_items_from_json(str(row["items_json"])),
        )
        for row in rows
    )
    if not orders:
        return None
    summary = CustomerSummary(
        customer_id=customer_id,
        order_count=len(orders),
        first_order_at=min(order.ordered_at for order in orders),
        last_order_at=max(order.ordered_at for order in orders),
    )
    return CustomerHistory(summary=summary, orders=orders)
