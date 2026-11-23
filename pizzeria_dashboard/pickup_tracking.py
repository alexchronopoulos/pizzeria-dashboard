"""Counter observations, compared with the pickup promised at the time of the tap."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


PICKUP_EVENTS = ("arrived", "picked_up")
ON_TIME_MINUTES = 5
RECENT_ARRIVALS = 10
MIN_TREND_ARRIVALS = 3


def timing_difference(actual: datetime, promised: datetime) -> tuple[int, str]:
    actual = actual.astimezone(UTC).replace(second=0, microsecond=0)
    promised = promised.astimezone(UTC).replace(second=0, microsecond=0)
    minutes = (actual - promised) // timedelta(minutes=1)
    status = "on-time" if abs(minutes) <= ON_TIME_MINUTES else ("early" if minutes < 0 else "late")
    return minutes, status


def arrival_pattern(statuses: list[str]) -> dict[str, str] | None:
    if len(statuses) < MIN_TREND_ARRIVALS:
        return None
    status, count = Counter(statuses).most_common(1)[0]
    if count * 3 < len(statuses) * 2:
        return None
    label = status.replace("-", " ")
    return {"status": status, "label": f"Usually {label}",
            "basis": f"{count} of {len(statuses)} prior recorded arrivals were {label}."}


@dataclass(frozen=True, slots=True)
class PickupMark:
    recorded_at: datetime
    scheduled_at: datetime | None

    def view(self, service_date: date, zone: ZoneInfo) -> dict[str, str]:
        def clock(value: datetime) -> str:
            local = value.astimezone(zone)
            return local.strftime("%-I:%M %p" if local.date() == service_date else "%b %-d, %-I:%M %p")

        status, offset, schedule = "unscheduled", "No scheduled time", ""
        if self.scheduled_at is not None:
            # Compare at the same minute precision staff see on the card. Use
            # instants, not time-of-day subtraction (midnight / DST changes).
            minutes, status = timing_difference(self.recorded_at, self.scheduled_at)
            side = "early" if minutes < 0 else "late"
            offset = f"{abs(minutes)} min {side}" if minutes else ""
            if status == "on-time":
                offset = f"On time · {offset}" if offset else "On time"
            schedule = f"Scheduled for {clock(self.scheduled_at)}"
        return {
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(),
            "clock": clock(self.recorded_at),
            "status": status,
            "offset": offset,
            "schedule": schedule,
        }
