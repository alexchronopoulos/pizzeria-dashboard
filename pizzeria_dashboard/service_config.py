from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping

from .database import load_app_metadata, save_app_metadata


CONFIGURATION_KEY = "service_configuration_v1"
DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
DEFAULT_SALAD_TYPES = (
    "Cucumber Salad",
    "Kale Caesar Salad",
)
DEFAULT_SIDE_TYPES = (
    "Side Ranch",
    "Side Hot Honey",
)


@dataclass(frozen=True, slots=True)
class DayHours:
    weekday: int
    name: str
    enabled: bool
    start: time
    end: time

    @property
    def start_value(self) -> str:
        return self.start.strftime("%H:%M")

    @property
    def end_value(self) -> str:
        return self.end.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    days: tuple[DayHours, ...]
    salad_types: tuple[str, ...]
    side_types: tuple[str, ...] = ()
    slot_minutes: int = 15
    pizzas_per_online_order_slot: int = 2

    def hours_for_date(self, service_date: date) -> DayHours:
        return self.days[service_date.weekday()]

    def pickup_times(self, service_date: date) -> tuple[datetime, ...]:
        hours = self.hours_for_date(service_date)
        if not hours.enabled or hours.end <= hours.start:
            return ()

        current = datetime.combine(service_date, hours.start)
        end = datetime.combine(service_date, hours.end)
        times: list[datetime] = []
        while current < end:
            times.append(current)
            current += timedelta(minutes=max(self.slot_minutes, 1))
        return tuple(times)


def _parse_time(value: object, fallback: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


def _deduplicate_names(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    seen: set[str] = set()
    names: list[str] = []
    for raw in values:
        name = " ".join(str(raw).split()).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return tuple(names)


def default_configuration() -> ServiceConfiguration:
    defaults: dict[int, tuple[bool, time, time]] = {
        0: (False, time(11, 0), time(20, 0)),
        1: (False, time(11, 0), time(20, 0)),
        2: (False, time(11, 0), time(20, 0)),
        3: (True, time(16, 0), time(20, 0)),
        4: (True, time(16, 0), time(20, 0)),
        5: (True, time(11, 0), time(20, 0)),
        6: (True, time(11, 0), time(16, 0)),
    }
    return ServiceConfiguration(
        days=tuple(
            DayHours(index, DAY_NAMES[index], *defaults[index]) for index in range(7)
        ),
        salad_types=DEFAULT_SALAD_TYPES,
        side_types=DEFAULT_SIDE_TYPES,
    )


def _configuration_from_payload(raw: object) -> ServiceConfiguration:
    defaults = default_configuration()
    if not isinstance(raw, dict):
        return defaults

    raw_days = raw.get("days")
    days: list[DayHours] = []
    for default_day in defaults.days:
        day_payload: object = None
        if isinstance(raw_days, dict):
            day_payload = raw_days.get(str(default_day.weekday))
        if not isinstance(day_payload, dict):
            days.append(default_day)
            continue
        days.append(
            DayHours(
                weekday=default_day.weekday,
                name=default_day.name,
                enabled=bool(day_payload.get("enabled", default_day.enabled)),
                start=_parse_time(day_payload.get("start"), default_day.start),
                end=_parse_time(day_payload.get("end"), default_day.end),
            )
        )

    salad_types = _deduplicate_names(raw.get("salad_types"))
    if "salad_types" not in raw:
        salad_types = defaults.salad_types

    side_types = _deduplicate_names(raw.get("side_types"))
    if "side_types" not in raw:
        side_types = defaults.side_types

    try:
        slot_minutes = max(int(raw.get("slot_minutes", defaults.slot_minutes)), 1)
    except (TypeError, ValueError):
        slot_minutes = defaults.slot_minutes

    # Current setting: each 15-minute pickup slot can expose a configurable
    # number of pizzas to online ordering. One pizza always consumes one dough
    # ball. Older installations stored this as ``online_order_slots_per_window``;
    # that field represented the same numeric capacity while the short-lived
    # ``online_order_dough_per_slot`` setting is intentionally ignored.
    legacy_online_capacity = raw.get(
        "online_order_slots_per_window", defaults.pizzas_per_online_order_slot
    )
    try:
        pizzas_per_online_order_slot = max(
            int(
                raw.get(
                    "pizzas_per_online_order_slot",
                    legacy_online_capacity,
                )
            ),
            0,
        )
    except (TypeError, ValueError):
        pizzas_per_online_order_slot = defaults.pizzas_per_online_order_slot

    return ServiceConfiguration(
        tuple(days),
        salad_types,
        side_types,
        slot_minutes,
        pizzas_per_online_order_slot,
    )


def load_configuration(path: Path) -> ServiceConfiguration:
    raw = load_app_metadata(path, CONFIGURATION_KEY)
    if raw is None:
        return default_configuration()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return default_configuration()
    return _configuration_from_payload(payload)


def save_configuration(path: Path, configuration: ServiceConfiguration) -> None:
    payload = {
        "days": {
            str(day.weekday): {
                "enabled": day.enabled,
                "start": day.start_value,
                "end": day.end_value,
            }
            for day in configuration.days
        },
        "salad_types": list(configuration.salad_types),
        "side_types": list(configuration.side_types),
        "slot_minutes": configuration.slot_minutes,
        "pizzas_per_online_order_slot": configuration.pizzas_per_online_order_slot,
    }
    save_app_metadata(
        path,
        CONFIGURATION_KEY,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def configuration_from_form(form: Mapping[str, str]) -> ServiceConfiguration:
    defaults = default_configuration()
    days: list[DayHours] = []
    for default_day in defaults.days:
        prefix = f"day_{default_day.weekday}"
        days.append(
            DayHours(
                weekday=default_day.weekday,
                name=default_day.name,
                enabled=f"{prefix}_enabled" in form,
                start=_parse_time(form.get(f"{prefix}_start"), default_day.start),
                end=_parse_time(form.get(f"{prefix}_end"), default_day.end),
            )
        )

    if "salad_types" in form:
        salad_text = str(form.get("salad_types", ""))
        salad_types = _deduplicate_names(salad_text.splitlines())
    else:
        salad_types = defaults.salad_types

    if "side_types" in form:
        side_text = str(form.get("side_types", ""))
        side_types = _deduplicate_names(side_text.splitlines())
    else:
        side_types = defaults.side_types
    try:
        pizzas_per_online_order_slot = max(
            int(
                form.get(
                    "pizzas_per_online_order_slot",
                    defaults.pizzas_per_online_order_slot,
                )
            ),
            0,
        )
    except (TypeError, ValueError):
        pizzas_per_online_order_slot = defaults.pizzas_per_online_order_slot

    return ServiceConfiguration(
        tuple(days),
        salad_types,
        side_types,
        defaults.slot_minutes,
        pizzas_per_online_order_slot,
    )
