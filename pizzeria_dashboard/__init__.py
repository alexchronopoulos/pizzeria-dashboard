from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, request

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


def _env_list(name: str) -> list[str] | None:
    values = [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]
    return values or None


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """Create and configure the Flask application."""
    load_dotenv(Path.cwd() / ".env")

    app = Flask(__name__, instance_relative_config=True)
    data_directory = Path(app.root_path).parent / "data"
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        DASHBOARD_AUTH_USERNAME=os.getenv("DASHBOARD_AUTH_USERNAME", ""),
        DASHBOARD_AUTH_PASSWORD=os.getenv("DASHBOARD_AUTH_PASSWORD", ""),
        DASHBOARD_HSTS=_env_bool("DASHBOARD_HSTS", False),
        TRUSTED_HOSTS=_env_list("DASHBOARD_TRUSTED_HOSTS"),
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
        SQUARE_AUTO_REFRESH_SECONDS=_env_int("SQUARE_AUTO_REFRESH_SECONDS", 10),
        SQUARE_INCREMENTAL_OVERLAP_SECONDS=_env_int(
            "SQUARE_INCREMENTAL_OVERLAP_SECONDS", 120
        ),
        CUSTOMER_HISTORY_START_DATE=os.getenv(
            "CUSTOMER_HISTORY_START_DATE", "2025-01-01"
        ),
        CUSTOMER_HISTORY_REFRESH_SECONDS=_env_int(
            "CUSTOMER_HISTORY_REFRESH_SECONDS", 60
        ),
        CUSTOMER_HISTORY_OVERLAP_HOURS=_env_int(
            "CUSTOMER_HISTORY_OVERLAP_HOURS", 48
        ),
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
        SQUARE_SLICE_CATEGORY_NAMES=os.getenv(
            "SQUARE_SLICE_CATEGORY_NAMES", "Slice,Slices"
        ),
        SQUARE_SLICE_ITEM_KEYWORDS=os.getenv(
            "SQUARE_SLICE_ITEM_KEYWORDS", "slice,slices"
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

    auth_username = str(app.config.get("DASHBOARD_AUTH_USERNAME", "")).strip()
    auth_password = str(app.config.get("DASHBOARD_AUTH_PASSWORD", ""))
    if bool(auth_username) != bool(auth_password):
        raise RuntimeError(
            "Set both DASHBOARD_AUTH_USERNAME and DASHBOARD_AUTH_PASSWORD, or leave both blank."
        )

    @app.before_request
    def _require_dashboard_authentication():
        if request.path == "/healthz":
            return None
        if not auth_username:
            return None
        authorization = request.authorization
        supplied_username = authorization.username if authorization else ""
        supplied_password = authorization.password if authorization else ""
        if secrets.compare_digest(supplied_username or "", auth_username) and secrets.compare_digest(
            supplied_password or "", auth_password
        ):
            return None
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Pizzeria Mari Dashboard", charset="UTF-8"'},
        )

    @app.after_request
    def _apply_dashboard_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        if app.config.get("DASHBOARD_HSTS"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    database_path = Path(app.config["DATABASE_PATH"])
    initialize_database(database_path)
    migrate_legacy_service_state(
        database_path,
        Path(app.config["LEGACY_SERVICE_STATE_PATH"]),
    )

    from .dashboard import blueprint

    app.register_blueprint(blueprint)
    return app
