from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .domain import Order, order_from_payload, order_to_payload


SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class SyncInfo:
    service_date: date
    source: str
    synced_at: datetime
    order_count: int


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


def load_order_slot_assignments(
    path: Path, service_date: date
) -> dict[str, datetime]:
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT order_id, pickup_at
            FROM order_slot_assignments
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchall()

    assignments: dict[str, datetime] = {}
    for row in rows:
        try:
            assignments[str(row["order_id"])] = datetime.fromisoformat(
                str(row["pickup_at"])
            )
        except ValueError:
            continue
    return assignments


def save_order_slot_assignment(
    path: Path,
    service_date: date,
    order_id: str,
    pickup_at: datetime | None,
) -> None:
    with _connect(path) as connection:
        if pickup_at is None:
            connection.execute(
                """
                DELETE FROM order_slot_assignments
                WHERE service_date = ? AND order_id = ?
                """,
                (service_date.isoformat(), order_id),
            )
            return
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
                pickup_at.isoformat(),
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
