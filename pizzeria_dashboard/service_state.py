from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .sample_data import SALAD_TYPES, ServiceBoard


DEFAULT_DOUGH_BALLS = 24
DEFAULT_SALAD_PREPARED = {
    "Cucumber Salad": 8,
    "Kale Caesar Salad": 8,
}


@dataclass(frozen=True, slots=True)
class ServiceState:
    dough_balls_prepared: int
    salad_prepared: dict[str, int]


@dataclass(frozen=True, slots=True)
class SaladStock:
    name: str
    field_name: str
    prepared: int
    ordered: int
    remaining: int


@dataclass(frozen=True, slots=True)
class InventorySummary:
    dough_prepared: int
    dough_ordered: int
    dough_remaining: int
    salads: tuple[SaladStock, ...]


def salad_field_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"salad_{slug}"


def _nonnegative_int(value: object, fallback: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return fallback


def default_state() -> ServiceState:
    return ServiceState(
        dough_balls_prepared=DEFAULT_DOUGH_BALLS,
        salad_prepared=dict(DEFAULT_SALAD_PREPARED),
    )


def _state_from_payload(raw: object) -> ServiceState:
    defaults = default_state()
    if not isinstance(raw, dict):
        return defaults

    raw_salads = raw.get("salad_prepared", {})
    if not isinstance(raw_salads, dict):
        raw_salads = {}

    salads = {
        name: _nonnegative_int(raw_salads.get(name), defaults.salad_prepared.get(name, 0))
        for name in SALAD_TYPES
    }
    return ServiceState(
        dough_balls_prepared=_nonnegative_int(
            raw.get("dough_balls_prepared"), defaults.dough_balls_prepared
        ),
        salad_prepared=salads,
    )


def _read_store(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "services": {}}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "services": {}}

    if isinstance(raw, dict) and isinstance(raw.get("services"), dict):
        return raw

    # Backward compatibility with the pre-date-selector single-service file.
    if isinstance(raw, dict) and (
        "dough_balls_prepared" in raw or "salad_prepared" in raw
    ):
        return {"version": 1, "services": {}, "legacy_state": raw}

    return {"version": 1, "services": {}}


def load_state(path: Path, service_date: date | None = None) -> ServiceState:
    selected_date = service_date or date.today()
    store = _read_store(path)
    services = store.get("services", {})
    if isinstance(services, dict) and selected_date.isoformat() in services:
        return _state_from_payload(services[selected_date.isoformat()])

    legacy_state = store.get("legacy_state")
    if legacy_state is not None:
        return _state_from_payload(legacy_state)

    return default_state()


def save_state(path: Path, service_date: date, state: ServiceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    store = _read_store(path)
    services = store.get("services")
    if not isinstance(services, dict):
        services = {}

    # If an old single-service file exists, preserve it under the date currently
    # being edited rather than silently discarding the operator's prep counts.
    if not services and store.get("legacy_state") is not None:
        services[service_date.isoformat()] = store["legacy_state"]

    services[service_date.isoformat()] = {
        "dough_balls_prepared": state.dough_balls_prepared,
        "salad_prepared": state.salad_prepared,
    }
    payload = {"version": 1, "services": services}

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def state_from_form(form: Mapping[str, str]) -> ServiceState:
    defaults = default_state()
    return ServiceState(
        dough_balls_prepared=_nonnegative_int(
            form.get("dough_balls_prepared"), defaults.dough_balls_prepared
        ),
        salad_prepared={
            name: _nonnegative_int(
                form.get(salad_field_name(name)), defaults.salad_prepared.get(name, 0)
            )
            for name in SALAD_TYPES
        },
    )


def build_inventory_summary(
    service: ServiceBoard,
    state: ServiceState,
) -> InventorySummary:
    salad_counts = service.salad_counts
    salads = tuple(
        SaladStock(
            name=name,
            field_name=salad_field_name(name),
            prepared=state.salad_prepared.get(name, 0),
            ordered=salad_counts.get(name, 0),
            remaining=state.salad_prepared.get(name, 0) - salad_counts.get(name, 0),
        )
        for name in SALAD_TYPES
    )
    return InventorySummary(
        dough_prepared=state.dough_balls_prepared,
        dough_ordered=service.total_pizzas,
        dough_remaining=state.dough_balls_prepared - service.total_pizzas,
        salads=salads,
    )
