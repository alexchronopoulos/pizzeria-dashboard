from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask

from .database import initialize_database, migrate_legacy_service_state
from .square_api import DEFAULT_SQUARE_API_VERSION


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """Create and configure the Flask application."""
    load_dotenv(Path.cwd() / ".env")

    app = Flask(__name__, instance_relative_config=True)
    data_directory = Path(app.root_path).parent / "data"
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "development-only-change-me"),
        SERVICE_TIMEZONE=os.getenv("SERVICE_TIMEZONE", "America/New_York"),
        DATABASE_PATH=str(data_directory / "pizza_dashboard.db"),
        LEGACY_SERVICE_STATE_PATH=str(data_directory / "service_state.json"),
        PIZZA_CAPACITY_PER_WINDOW=_env_int("PIZZA_CAPACITY_PER_WINDOW", 3),
        ORDER_SOURCE=os.getenv("ORDER_SOURCE", "auto"),
        AUTO_SEED_SAMPLE_DATA=_env_bool("AUTO_SEED_SAMPLE_DATA", True),
        SQUARE_ACCESS_TOKEN=os.getenv("SQUARE_ACCESS_TOKEN", ""),
        SQUARE_LOCATION_ID=os.getenv("SQUARE_LOCATION_ID", ""),
        SQUARE_ENVIRONMENT=os.getenv("SQUARE_ENVIRONMENT", "production"),
        SQUARE_API_VERSION=os.getenv(
            "SQUARE_API_VERSION", DEFAULT_SQUARE_API_VERSION
        ),
        SQUARE_TIMEOUT_SECONDS=_env_int("SQUARE_TIMEOUT_SECONDS", 20),
        SQUARE_ORDER_LOOKBACK_DAYS=_env_int("SQUARE_ORDER_LOOKBACK_DAYS", 60),
        SQUARE_PIZZA_CATEGORY_NAMES=os.getenv(
            "SQUARE_PIZZA_CATEGORY_NAMES",
            "Traditional Pies,Mari Pies,Seasonal Special Pies,Pizza,Pizzas",
        ),
        SQUARE_HIDDEN_CATEGORY_NAMES=os.getenv(
            "SQUARE_HIDDEN_CATEGORY_NAMES", "Drink,Drinks,Beverage,Beverages"
        ),
        SQUARE_PIZZA_ITEM_KEYWORDS=os.getenv(
            "SQUARE_PIZZA_ITEM_KEYWORDS", "pizza,pie"
        ),
        SQUARE_HIDDEN_ITEM_KEYWORDS=os.getenv(
            "SQUARE_HIDDEN_ITEM_KEYWORDS", "drink,beverage,coke,soda,water"
        ),
        SQUARE_SALAD_MODIFIER_KEYWORDS=os.getenv(
            "SQUARE_SALAD_MODIFIER_KEYWORDS", "salad"
        ),
        SQUARE_SIDE_MODIFIER_KEYWORDS=os.getenv(
            "SQUARE_SIDE_MODIFIER_KEYWORDS", "side"
        ),
        SQUARE_COOKIE_MODIFIER_KEYWORDS=os.getenv(
            "SQUARE_COOKIE_MODIFIER_KEYWORDS", "cookie"
        ),
        SQUARE_COOKIE_CATEGORY_NAMES=os.getenv(
            "SQUARE_COOKIE_CATEGORY_NAMES", "Cookie,Cookies,Dessert,Desserts"
        ),
        SQUARE_COOKIE_ITEM_KEYWORDS=os.getenv(
            "SQUARE_COOKIE_ITEM_KEYWORDS", "cookie"
        ),
    )

    if test_config:
        app.config.update(test_config)

    database_path = Path(app.config["DATABASE_PATH"])
    initialize_database(database_path)
    migrate_legacy_service_state(
        database_path,
        Path(app.config["LEGACY_SERVICE_STATE_PATH"]),
    )

    from .dashboard import blueprint

    app.register_blueprint(blueprint)
    return app
