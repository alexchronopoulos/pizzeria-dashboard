from datetime import date
from pathlib import Path

from pizzeria_dashboard.database import initialize_database
from pizzeria_dashboard.service_config import (
    configuration_from_form,
    default_configuration,
    load_configuration,
    save_configuration,
)


def test_default_schedule_matches_current_service_hours() -> None:
    config = default_configuration()
    assert len(config.pickup_times(date(2026, 7, 30))) == 16  # Thursday 4–8
    assert len(config.pickup_times(date(2026, 8, 1))) == 36  # Saturday 11–8
    assert len(config.pickup_times(date(2026, 8, 2))) == 20  # Sunday 11–4
    assert config.pickup_times(date(2026, 7, 29)) == ()  # Wednesday closed
    assert config.pickup_times(date(2026, 7, 30))[-1].strftime("%H:%M") == "19:45"


def test_configuration_round_trips_through_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "dashboard.db"
    initialize_database(database_path)
    config = configuration_from_form(
        {
            "day_3_enabled": "on",
            "day_3_start": "17:00",
            "day_3_end": "19:00",
            "salad_types": "Tomato Salad\nTomato Salad\nLittle Gem Salad",
        }
    )
    save_configuration(database_path, config)
    loaded = load_configuration(database_path)
    assert loaded.days[3].enabled is True
    assert loaded.days[3].start_value == "17:00"
    assert loaded.days[3].end_value == "19:00"
    assert loaded.salad_types == ("Tomato Salad", "Little Gem Salad")
    assert loaded.side_types == ("Side Ranch", "Side Hot Honey")
