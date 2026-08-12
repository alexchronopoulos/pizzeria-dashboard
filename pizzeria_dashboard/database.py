from __future__ import annotations

import json
import sqlite3
import time
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


SCHEMA_VERSION = 9

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


@dataclass(frozen=True, slots=True)
class PieProductionState:
    pie_key: str
    timer_status: str
    timer_remaining_ms: int
    timer_end_at_ms: int | None
    oven_position: str | None
    updated_at: datetime


OVEN_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
TIMER_STATUSES = {"idle", "running", "paused", "done"}


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

            CREATE TABLE IF NOT EXISTS manual_orders (
                order_id TEXT NOT NULL,
                service_date TEXT NOT NULL,
                pickup_at TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                source_payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (order_id, service_date)
            );

            CREATE INDEX IF NOT EXISTS idx_manual_orders_service_pickup
                ON manual_orders (service_date, pickup_at);

            CREATE TABLE IF NOT EXISTS dashboard_hidden_orders (
                service_date TEXT NOT NULL,
                order_id TEXT NOT NULL,
                hidden_at TEXT NOT NULL,
                PRIMARY KEY (service_date, order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_dashboard_hidden_orders_service_date
                ON dashboard_hidden_orders (service_date);

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

            CREATE TABLE IF NOT EXISTS order_internal_notes (
                service_date TEXT NOT NULL,
                order_id TEXT NOT NULL,
                note TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (service_date, order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_order_internal_notes_service_date
                ON order_internal_notes (service_date);

            CREATE TABLE IF NOT EXISTS pie_production_states (
                service_date TEXT NOT NULL,
                pie_key TEXT NOT NULL,
                timer_status TEXT NOT NULL DEFAULT 'idle',
                timer_remaining_ms INTEGER NOT NULL DEFAULT 480000,
                timer_end_at_ms INTEGER,
                oven_position TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (service_date, pie_key)
            );

            CREATE INDEX IF NOT EXISTS idx_pie_production_states_service_date
                ON pie_production_states (service_date);

            CREATE TABLE IF NOT EXISTS order_ready_states (
                service_date TEXT NOT NULL,
                order_id TEXT NOT NULL,
                boxed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (service_date, order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_order_ready_states_service_date
                ON order_ready_states (service_date);

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

    serialized_orders: dict[str, str] = {
        order.order_id: json.dumps(
            order_to_payload(order),
            separators=(",", ":"),
            sort_keys=True,
        )
        for order in order_list
    }

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

        for order in order_list:
            payload = serialized_orders[order.order_id]
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
                  AND order_id NOT IN (
                      SELECT order_id FROM manual_orders WHERE service_date = ?
                  )
                """,
                (
                    date_key,
                    *(order.order_id for order in order_list),
                    date_key,
                ),
            )
        else:
            connection.execute(
                "DELETE FROM orders WHERE service_date = ?",
                (date_key,),
            )
            connection.execute(
                """
                DELETE FROM order_slot_assignments
                WHERE service_date = ?
                  AND order_id NOT IN (
                      SELECT order_id FROM manual_orders WHERE service_date = ?
                  )
                """,
                (date_key, date_key),
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
        # Only publish a shared revision when the actual board content changed.
        # Sync timestamps alone should not make other displays reload; this also
        # preserves a staff-note revision across an otherwise no-op full refresh.
        if existing_payloads != serialized_orders:
            _touch_board_content_revision(connection, date_key)

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
        if changed_count or removed_count:
            _touch_board_content_revision(connection, date_key)

    return MergeResult(
        info=SyncInfo(service_date, source, sync_time, order_count),
        changed_count=changed_count,
        removed_count=removed_count,
    )


def save_manual_order(
    path: Path, service_date: date, order: Order
) -> Order:
    """Persist one dashboard-only order independently from the Square cache."""
    if order.pickup_at.date() != service_date:
        raise ValueError("Manual order pickup date must match the service date.")
    payload = json.dumps(
        order_to_payload(order),
        separators=(",", ":"),
        sort_keys=True,
    )
    now = _utc_now().isoformat()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT source_payload_json
            FROM manual_orders
            WHERE service_date = ? AND order_id = ?
            """,
            (service_date.isoformat(), order.order_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO manual_orders (
                order_id, service_date, pickup_at, customer_name,
                source_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id, service_date) DO UPDATE SET
                pickup_at = excluded.pickup_at,
                customer_name = excluded.customer_name,
                source_payload_json = excluded.source_payload_json,
                updated_at = excluded.updated_at
            """,
            (
                order.order_id,
                service_date.isoformat(),
                order.pickup_at.isoformat(),
                order.customer_name,
                payload,
                now,
                now,
            ),
        )
        if existing is None or str(existing["source_payload_json"]) != payload:
            _touch_board_content_revision(connection, service_date.isoformat())
    return order


def load_manual_orders_for_date(
    path: Path, service_date: date
) -> tuple[Order, ...]:
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT source_payload_json
            FROM manual_orders
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


def load_orders_for_date(path: Path, service_date: date) -> tuple[Order, ...]:
    """Load Square-cached and dashboard-only manual orders for one service date."""
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT o.pickup_at, o.order_id, o.source_payload_json
            FROM orders AS o
            WHERE o.service_date = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM dashboard_hidden_orders AS hidden
                  WHERE hidden.service_date = o.service_date
                    AND hidden.order_id = o.order_id
              )
            UNION ALL
            SELECT pickup_at, order_id, source_payload_json
            FROM manual_orders
            WHERE service_date = ?
            ORDER BY pickup_at, order_id
            """,
            (service_date.isoformat(), service_date.isoformat()),
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
    """Load one Square-cached or dashboard-only order for the details view."""
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT o.source_payload_json
            FROM orders AS o
            WHERE o.service_date = ? AND o.order_id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM dashboard_hidden_orders AS hidden
                  WHERE hidden.service_date = o.service_date
                    AND hidden.order_id = o.order_id
              )
            UNION ALL
            SELECT source_payload_json
            FROM manual_orders
            WHERE service_date = ? AND order_id = ?
            LIMIT 1
            """,
            (
                service_date.isoformat(),
                order_id,
                service_date.isoformat(),
                order_id,
            ),
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


def remove_order_from_dashboard(
    path: Path, service_date: date, order_id: str
) -> str | None:
    """Remove one order from the local production board without touching Square.

    Manual orders are deleted from their dashboard-only table. Source-backed orders
    receive a local tombstone so future full/incremental source refreshes cannot make
    them reappear. Returns ``"manual"``, ``"hidden"``, or ``None`` when no
    matching visible order exists.
    """
    date_key = service_date.isoformat()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        manual_row = connection.execute(
            """
            SELECT source_payload_json
            FROM manual_orders
            WHERE service_date = ? AND order_id = ?
            """,
            (date_key, order_id),
        ).fetchone()
        source_row = None
        if manual_row is None:
            source_row = connection.execute(
                """
                SELECT source_payload_json
                FROM orders
                WHERE service_date = ? AND order_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dashboard_hidden_orders
                      WHERE service_date = ? AND order_id = ?
                  )
                """,
                (date_key, order_id, date_key, order_id),
            ).fetchone()

        row = manual_row or source_row
        if row is None:
            return None

        production_order_key = order_id
        try:
            payload = json.loads(row["source_payload_json"])
            if isinstance(payload, Mapping):
                stored_order = order_from_payload(payload)
                production_order_key = stored_order.square_order_id or stored_order.order_id
        except (TypeError, json.JSONDecodeError, ValueError):
            pass

        if manual_row is not None:
            connection.execute(
                "DELETE FROM manual_orders WHERE service_date = ? AND order_id = ?",
                (date_key, order_id),
            )
            result = "manual"
        else:
            connection.execute(
                """
                INSERT INTO dashboard_hidden_orders (service_date, order_id, hidden_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service_date, order_id) DO UPDATE SET
                    hidden_at = excluded.hidden_at
                """,
                (date_key, order_id, _utc_now().isoformat()),
            )
            result = "hidden"

        for table in (
            "order_slot_assignments",
            "order_internal_notes",
            "order_ready_states",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE service_date = ? AND order_id = ?",
                (date_key, order_id),
            )

        pie_prefix = f"{date_key}|{production_order_key}|"
        connection.execute(
            """
            DELETE FROM pie_production_states
            WHERE service_date = ?
              AND substr(pie_key, 1, ?) = ?
            """,
            (date_key, len(pie_prefix), pie_prefix),
        )
        _touch_board_content_revision(connection, date_key)
        return result


def load_order_internal_notes_for_date(
    path: Path, service_date: date
) -> dict[str, str]:
    """Load staff-authored notes for orders on one service date."""
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT order_id, note
            FROM order_internal_notes
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchall()
    return {str(row["order_id"]): str(row["note"]) for row in rows}


def load_order_internal_note(
    path: Path, service_date: date, order_id: str
) -> str | None:
    """Load one staff-authored order note."""
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT note
            FROM order_internal_notes
            WHERE service_date = ? AND order_id = ?
            """,
            (service_date.isoformat(), order_id),
        ).fetchone()
    return str(row["note"]) if row is not None else None


def save_order_internal_note(
    path: Path, service_date: date, order_id: str, note: str
) -> str | None:
    """Save or clear a staff note without modifying the cached Square order."""
    normalized = str(note).replace("\r\n", "\n").replace("\r", "\n").strip()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if normalized:
            connection.execute(
                """
                INSERT INTO order_internal_notes (
                    service_date, order_id, note, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(service_date, order_id) DO UPDATE SET
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    service_date.isoformat(),
                    order_id,
                    normalized,
                    _utc_now().isoformat(),
                ),
            )
        else:
            connection.execute(
                """
                DELETE FROM order_internal_notes
                WHERE service_date = ? AND order_id = ?
                """,
                (service_date.isoformat(), order_id),
            )
        _touch_board_content_revision(connection, service_date.isoformat())
    return normalized or None


def _touch_board_content_revision(
    connection: sqlite3.Connection, service_date_key: str
) -> str:
    revision = str(time.time_ns())
    connection.execute(
        """
        INSERT INTO app_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (f"board_content_revision:{service_date_key}", revision),
    )
    return revision


def load_board_content_revision(path: Path, service_date: date) -> str:
    """Return a monotonic token for local changes that require HTML reload."""
    with _connect(path) as connection:
        return _metadata_value(
            connection, f"board_content_revision:{service_date.isoformat()}"
        ) or ""


def _epoch_ms(value: datetime | None = None) -> int:
    return int((value or _utc_now()).timestamp() * 1000)


def _expire_completed_pie_timers(
    connection: sqlite3.Connection, service_date_key: str, *, now: datetime
) -> int:
    """Persist completed timers and free their shared oven positions."""
    cursor = connection.execute(
        """
        UPDATE pie_production_states
        SET timer_status = 'done',
            timer_remaining_ms = 0,
            timer_end_at_ms = NULL,
            oven_position = NULL,
            updated_at = ?
        WHERE service_date = ?
          AND timer_status = 'running'
          AND timer_end_at_ms IS NOT NULL
          AND timer_end_at_ms <= ?
        """,
        (now.isoformat(), service_date_key, _epoch_ms(now)),
    )
    return max(cursor.rowcount, 0)


def _normalized_pie_state(
    row: sqlite3.Row | None,
    pie_key: str,
    *,
    now_ms: int,
    default_duration_ms: int = 480_000,
) -> PieProductionState:
    if row is None:
        return PieProductionState(
            pie_key=pie_key,
            timer_status="idle",
            timer_remaining_ms=default_duration_ms,
            timer_end_at_ms=None,
            oven_position=None,
            updated_at=_utc_now(),
        )
    status = str(row["timer_status"] or "idle")
    if status not in TIMER_STATUSES:
        status = "idle"
    remaining_ms = max(int(row["timer_remaining_ms"] or 0), 0)
    end_at_ms = int(row["timer_end_at_ms"]) if row["timer_end_at_ms"] is not None else None
    if status == "running" and end_at_ms is not None:
        remaining_ms = max(end_at_ms - now_ms, 0)
        if remaining_ms == 0:
            status = "done"
            end_at_ms = None
    oven_position = str(row["oven_position"]) if row["oven_position"] else None
    if oven_position not in OVEN_POSITIONS or status == "done":
        oven_position = None
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        updated_at = _utc_now()
    return PieProductionState(
        pie_key=pie_key,
        timer_status=status,
        timer_remaining_ms=remaining_ms,
        timer_end_at_ms=end_at_ms,
        oven_position=oven_position,
        updated_at=updated_at,
    )


def prune_pie_production_states(
    path: Path, service_date: date, valid_pie_keys: Iterable[str]
) -> int:
    valid = tuple(dict.fromkeys(str(key) for key in valid_pie_keys if key))
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if valid:
            placeholders = ",".join("?" for _ in valid)
            cursor = connection.execute(
                f"""
                DELETE FROM pie_production_states
                WHERE service_date = ? AND pie_key NOT IN ({placeholders})
                """,
                (service_date.isoformat(), *valid),
            )
        else:
            cursor = connection.execute(
                "DELETE FROM pie_production_states WHERE service_date = ?",
                (service_date.isoformat(),),
            )
    return max(cursor.rowcount, 0)


def load_pie_production_states(
    path: Path, service_date: date
) -> dict[str, PieProductionState]:
    now = _utc_now()
    now_ms = _epoch_ms(now)
    with _connect(path) as connection:
        _expire_completed_pie_timers(
            connection, service_date.isoformat(), now=now
        )
        rows = connection.execute(
            """
            SELECT pie_key, timer_status, timer_remaining_ms, timer_end_at_ms,
                   oven_position, updated_at
            FROM pie_production_states
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchall()
    return {
        str(row["pie_key"]): _normalized_pie_state(
            row, str(row["pie_key"]), now_ms=now_ms
        )
        for row in rows
    }


def update_pie_production_state(
    path: Path,
    service_date: date,
    pie_key: str,
    *,
    timer_action: str | None = None,
    duration_ms: int = 480_000,
    oven_position: str | None | object = ...,
) -> PieProductionState:
    """Update one shared pie timer/oven state atomically.

    Starting an idle timer automatically takes the first available oven position
    in top-left to bottom-right order. Passing ``oven_position=None`` clears the
    current position; omitting it leaves the position unchanged.
    """
    normalized_duration = min(max(int(duration_ms), 1_000), 3_600_000)
    normalized_action = (timer_action or "").strip().lower() or None
    if normalized_action not in {None, "start", "pause", "reset"}:
        raise ValueError("Unsupported timer action.")
    if oven_position is not ... and oven_position is not None and oven_position not in OVEN_POSITIONS:
        raise ValueError("Unsupported oven position.")

    date_key = service_date.isoformat()
    now = _utc_now()
    now_ms = _epoch_ms(now)
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _expire_completed_pie_timers(connection, date_key, now=now)
        row = connection.execute(
            """
            SELECT pie_key, timer_status, timer_remaining_ms, timer_end_at_ms,
                   oven_position, updated_at
            FROM pie_production_states
            WHERE service_date = ? AND pie_key = ?
            """,
            (date_key, pie_key),
        ).fetchone()
        state = _normalized_pie_state(
            row, pie_key, now_ms=now_ms, default_duration_ms=normalized_duration
        )
        status = state.timer_status
        remaining_ms = state.timer_remaining_ms
        end_at_ms = state.timer_end_at_ms
        selected_position = state.oven_position

        if normalized_action == "start":
            if status == "paused" and remaining_ms > 0:
                run_for_ms = remaining_ms
            else:
                run_for_ms = normalized_duration
            status = "running"
            remaining_ms = run_for_ms
            end_at_ms = now_ms + run_for_ms
            if selected_position is None:
                occupied_rows = connection.execute(
                    """
                    SELECT oven_position FROM pie_production_states
                    WHERE service_date = ? AND oven_position IS NOT NULL AND pie_key != ?
                    """,
                    (date_key, pie_key),
                ).fetchall()
                occupied = {str(value["oven_position"]) for value in occupied_rows}
                selected_position = next(
                    (position for position in OVEN_POSITIONS if position not in occupied),
                    None,
                )
        elif normalized_action == "pause":
            if status == "running" and end_at_ms is not None:
                remaining_ms = max(end_at_ms - now_ms, 0)
                status = "paused" if remaining_ms else "done"
                end_at_ms = None
        elif normalized_action == "reset":
            status = "idle"
            remaining_ms = normalized_duration
            end_at_ms = None
            selected_position = None

        if oven_position is not ...:
            selected_position = oven_position
            if selected_position is not None:
                connection.execute(
                    """
                    UPDATE pie_production_states
                    SET oven_position = NULL, updated_at = ?
                    WHERE service_date = ? AND oven_position = ? AND pie_key != ?
                    """,
                    (now.isoformat(), date_key, selected_position, pie_key),
                )

        if status == "done":
            selected_position = None

        connection.execute(
            """
            INSERT INTO pie_production_states (
                service_date, pie_key, timer_status, timer_remaining_ms,
                timer_end_at_ms, oven_position, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_date, pie_key) DO UPDATE SET
                timer_status = excluded.timer_status,
                timer_remaining_ms = excluded.timer_remaining_ms,
                timer_end_at_ms = excluded.timer_end_at_ms,
                oven_position = excluded.oven_position,
                updated_at = excluded.updated_at
            """,
            (
                date_key, pie_key, status, remaining_ms, end_at_ms,
                selected_position, now.isoformat(),
            ),
        )

    return PieProductionState(
        pie_key=pie_key,
        timer_status=status,
        timer_remaining_ms=remaining_ms,
        timer_end_at_ms=end_at_ms,
        oven_position=selected_position,
        updated_at=now,
    )


def load_order_ready_states(
    path: Path, service_date: date
) -> dict[str, datetime]:
    with _connect(path) as connection:
        rows = connection.execute(
            """
            SELECT order_id, boxed_at
            FROM order_ready_states
            WHERE service_date = ?
            """,
            (service_date.isoformat(),),
        ).fetchall()
    states: dict[str, datetime] = {}
    for row in rows:
        try:
            states[str(row["order_id"])] = datetime.fromisoformat(str(row["boxed_at"]))
        except ValueError:
            continue
    return states


def save_order_ready_state(
    path: Path, service_date: date, order_id: str, *, boxed: bool
) -> datetime | None:
    now = _utc_now()
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if boxed:
            connection.execute(
                """
                INSERT INTO order_ready_states (service_date, order_id, boxed_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(service_date, order_id) DO UPDATE SET
                    boxed_at = excluded.boxed_at,
                    updated_at = excluded.updated_at
                """,
                (service_date.isoformat(), order_id, now.isoformat(), now.isoformat()),
            )
            return now
        connection.execute(
            """DELETE FROM order_ready_states WHERE service_date = ? AND order_id = ?""",
            (service_date.isoformat(), order_id),
        )
    return None


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
    """Load local pickup-slot overrides for cached orders.

    Walk-ins may use ``None`` to remain explicitly unscheduled. Scheduled
    Square orders use a concrete timestamp only; deleting their row restores
    the original pickup time from the cached Square order.
    """
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
    """Load only orders assigned to actual service slots.

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
        _touch_board_content_revision(connection, service_date.isoformat())


def delete_order_slot_assignment(
    path: Path,
    service_date: date,
    order_id: str,
) -> None:
    """Remove a local slot override and restore the source pickup behavior."""
    with _connect(path) as connection:
        connection.execute(
            """
            DELETE FROM order_slot_assignments
            WHERE service_date = ? AND order_id = ?
            """,
            (service_date.isoformat(), order_id),
        )
        _touch_board_content_revision(connection, service_date.isoformat())


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


def load_latest_service_state_before(
    path: Path, service_date: date
) -> tuple[date, dict[str, object]] | None:
    """Return the most recent saved prep state before ``service_date``."""
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT service_date, state_json
            FROM service_states
            WHERE service_date < ?
            ORDER BY service_date DESC
            LIMIT 1
            """,
            (service_date.isoformat(),),
        ).fetchone()
    if row is None:
        return None
    try:
        previous_date = date.fromisoformat(str(row["service_date"]))
        payload = json.loads(str(row["state_json"]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return previous_date, payload


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
             AND history.ordered_at <= current.ordered_at
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
