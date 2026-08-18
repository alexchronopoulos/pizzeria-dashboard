from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .database import load_service_state_payload, save_service_state_payload
from .domain import Order, ServiceBoard, production_display_name
from .service_config import DEFAULT_SALAD_TYPES, DEFAULT_SIDE_TYPES


DEFAULT_DOUGH_BALLS = 24
DEFAULT_SALAD_PREPARED = {
    "Cucumber Salad": 8,
    "Kale Caesar Salad": 8,
}
DEFAULT_SIDE_PREPARED: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class ServiceState:
    dough_balls_prepared: int
    salad_prepared: dict[str, int]
    side_prepared: dict[str, int]
    cookie_prepared: int
    slice_pies: int = 0


@dataclass(frozen=True, slots=True)
class PreparedStock:
    name: str
    field_name: str
    prepared: int
    ordered: int
    remaining: int

    @property
    def display_name(self) -> str:
        return production_display_name(self.name)


@dataclass(frozen=True, slots=True)
class InventorySummary:
    dough_prepared: int
    dough_ordered: int
    dough_slice_pies: int
    dough_open_slot_reserve: int
    dough_remaining: int
    salads: tuple[PreparedStock, ...]
    sides: tuple[PreparedStock, ...]
    cookies: tuple[PreparedStock, ...]

    @property
    def dough_online_order_reserve(self) -> int:
        """Dough held for online-order slots that are still available."""
        return self.dough_open_slot_reserve

    @property
    def prepared_items(self) -> tuple[PreparedStock, ...]:
        return (*self.salads, *self.sides, *self.cookies)


def _field_name(prefix: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{prefix}_{slug}"


def salad_field_name(name: str) -> str:
    return _field_name("salad", name)


def side_field_name(name: str) -> str:
    return _field_name("side", name)


def _nonnegative_int(value: object, fallback: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return fallback


def _unique_names(names: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in names:
        name = " ".join(str(raw).split()).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return tuple(result)


def default_state(
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
) -> ServiceState:
    return ServiceState(
        dough_balls_prepared=DEFAULT_DOUGH_BALLS,
        salad_prepared={
            name: DEFAULT_SALAD_PREPARED.get(name, 0)
            for name in _unique_names(tuple(salad_types))
        },
        side_prepared={
            name: DEFAULT_SIDE_PREPARED.get(name, 0)
            for name in _unique_names(tuple(side_types))
        },
        cookie_prepared=0,
        slice_pies=0,
    )


def _prepared_values(
    raw_values: object,
    names: Sequence[str],
    defaults: Mapping[str, int],
) -> dict[str, int]:
    values = raw_values if isinstance(raw_values, dict) else {}
    return {
        name: _nonnegative_int(values.get(name), defaults.get(name, 0))
        for name in _unique_names(tuple(names))
    }


def _state_from_payload(
    raw: object,
    salad_types: Sequence[str],
    side_types: Sequence[str],
) -> ServiceState:
    defaults = default_state(salad_types, side_types)
    if not isinstance(raw, dict):
        return defaults

    return ServiceState(
        dough_balls_prepared=_nonnegative_int(
            raw.get("dough_balls_prepared"), defaults.dough_balls_prepared
        ),
        salad_prepared=_prepared_values(
            raw.get("salad_prepared"), salad_types, defaults.salad_prepared
        ),
        side_prepared=_prepared_values(
            raw.get("side_prepared"), side_types, defaults.side_prepared
        ),
        cookie_prepared=_nonnegative_int(
            raw.get("cookie_prepared"), defaults.cookie_prepared
        ),
        slice_pies=_nonnegative_int(raw.get("slice_pies"), defaults.slice_pies),
    )


def state_from_payload(
    raw: object,
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
) -> ServiceState:
    """Hydrate a service state payload using the current configured lineup."""
    return _state_from_payload(raw, salad_types, side_types)


def _inventory_demand_from_orders(
    orders: Iterable[Order],
) -> tuple[dict[str, int], dict[str, int], int]:
    salads: dict[str, int] = {}
    sides: dict[str, int] = {}
    cookies = 0
    for order in orders:
        for name, quantity in order.salad_counts.items():
            salads[name] = salads.get(name, 0) + quantity
        for name, quantity in order.side_counts.items():
            sides[name] = sides.get(name, 0) + quantity
        cookies += order.cookie_count
    return salads, sides, cookies


def _casefold_value(values: Mapping[str, int], name: str) -> int | None:
    wanted = name.casefold()
    for key, value in values.items():
        if key.casefold() == wanted:
            return value
    return None


def carryover_state(
    previous_state: ServiceState,
    previous_orders: Iterable[Order],
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
) -> ServiceState:
    """Start a new service day with yesterday's unsold prepared food.

    Dough is intentionally not carried because dough production is planned per
    service. Only currently configured salad/side names are inherited, so a
    removed menu item does not reappear on the next day's prep sheet.
    """
    salad_names = _unique_names(tuple(salad_types))
    side_names = _unique_names(tuple(side_types))
    demand_salads, demand_sides, demand_cookies = _inventory_demand_from_orders(
        tuple(previous_orders)
    )

    def remaining(
        prepared_values: Mapping[str, int], demand_values: Mapping[str, int], name: str
    ) -> int:
        prepared = _casefold_value(prepared_values, name)
        if prepared is None:
            return 0
        ordered = _casefold_value(demand_values, name) or 0
        return max(prepared - ordered, 0)

    defaults = default_state(salad_names, side_names)
    return ServiceState(
        dough_balls_prepared=defaults.dough_balls_prepared,
        salad_prepared={
            name: remaining(previous_state.salad_prepared, demand_salads, name)
            for name in salad_names
        },
        side_prepared={
            name: remaining(previous_state.side_prepared, demand_sides, name)
            for name in side_names
        },
        cookie_prepared=max(previous_state.cookie_prepared - demand_cookies, 0),
        slice_pies=0,
    )


def load_state(
    path: Path,
    service_date: date | None = None,
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
) -> ServiceState:
    selected_date = service_date or date.today()
    return _state_from_payload(
        load_service_state_payload(path, selected_date), salad_types, side_types
    )


def save_state(path: Path, service_date: date, state: ServiceState) -> None:
    save_service_state_payload(
        path,
        service_date,
        {
            "dough_balls_prepared": state.dough_balls_prepared,
            "salad_prepared": state.salad_prepared,
            "side_prepared": state.side_prepared,
            "cookie_prepared": state.cookie_prepared,
            "slice_pies": state.slice_pies,
        },
    )


def state_from_form(
    form: Mapping[str, str],
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
) -> ServiceState:
    salad_names = _unique_names(tuple(salad_types))
    side_names = _unique_names(tuple(side_types))
    defaults = default_state(salad_names, side_names)
    return ServiceState(
        dough_balls_prepared=_nonnegative_int(
            form.get("dough_balls_prepared"), defaults.dough_balls_prepared
        ),
        salad_prepared={
            name: _nonnegative_int(
                form.get(salad_field_name(name)),
                defaults.salad_prepared.get(name, 0),
            )
            for name in salad_names
        },
        side_prepared={
            name: _nonnegative_int(
                form.get(side_field_name(name)),
                defaults.side_prepared.get(name, 0),
            )
            for name in side_names
        },
        cookie_prepared=_nonnegative_int(
            form.get("cookie_prepared"), defaults.cookie_prepared
        ),
        slice_pies=_nonnegative_int(form.get("slice_pies"), defaults.slice_pies),
    )


def _stock_rows(
    *,
    configured_names: Sequence[str],
    observed_counts: Mapping[str, int],
    prepared_counts: Mapping[str, int],
    field_name,
) -> tuple[PreparedStock, ...]:
    configured = list(_unique_names(tuple(configured_names)))
    configured_keys = {name.casefold() for name in configured}
    observed = sorted(
        (name for name in observed_counts if name.casefold() not in configured_keys),
        key=str.casefold,
    )
    return tuple(
        PreparedStock(
            name=name,
            field_name=field_name(name),
            prepared=prepared_counts.get(name, 0),
            ordered=observed_counts.get(name, 0),
            remaining=prepared_counts.get(name, 0) - observed_counts.get(name, 0),
        )
        for name in (*configured, *observed)
    )


def build_inventory_summary(
    service: ServiceBoard,
    state: ServiceState,
    salad_types: Sequence[str] = DEFAULT_SALAD_TYPES,
    side_types: Sequence[str] = DEFAULT_SIDE_TYPES,
    *,
    orders: Iterable[Order] | None = None,
    open_slot_dough_reserve: int = 0,
) -> InventorySummary:
    if orders is None:
        observed_salads = dict(service.salad_counts)
        observed_sides = dict(service.side_counts)
        observed_cookies = service.total_cookies
    else:
        observed_salads, observed_sides, observed_cookies = _inventory_demand_from_orders(
            tuple(orders)
        )
    reserved_for_open_slots = max(int(open_slot_dough_reserve), 0)
    return InventorySummary(
        dough_prepared=state.dough_balls_prepared,
        dough_ordered=service.total_pizzas,
        dough_slice_pies=state.slice_pies,
        dough_open_slot_reserve=reserved_for_open_slots,
        dough_remaining=(
            state.dough_balls_prepared
            - service.total_pizzas
            - state.slice_pies
            - reserved_for_open_slots
        ),
        salads=_stock_rows(
            configured_names=salad_types,
            observed_counts=observed_salads,
            prepared_counts=state.salad_prepared,
            field_name=salad_field_name,
        ),
        sides=_stock_rows(
            configured_names=side_types,
            observed_counts=observed_sides,
            prepared_counts=state.side_prepared,
            field_name=side_field_name,
        ),
        cookies=(
            PreparedStock(
                name="Cookies",
                field_name="cookie_prepared",
                prepared=state.cookie_prepared,
                ordered=observed_cookies,
                remaining=state.cookie_prepared - observed_cookies,
            ),
        ),
    )
