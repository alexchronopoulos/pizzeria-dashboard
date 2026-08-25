from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for

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


def _load_or_create_secret_key(secret_key_path: Path) -> str:
    """Return a stable signing key without requiring another deployed secret."""
    secret_key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_key = secret_key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing_key = ""
    if existing_key:
        secret_key_path.chmod(0o600)
        return existing_key

    generated_key = secrets.token_hex(32)
    try:
        file_descriptor = os.open(
            secret_key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        existing_key = secret_key_path.read_text(encoding="utf-8").strip()
        if not existing_key:
            raise RuntimeError(f"Dashboard secret key file is empty: {secret_key_path}")
        return existing_key

    with os.fdopen(file_descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(generated_key)
    return generated_key


def _safe_local_url(candidate: str | None, fallback: str = "/") -> str:
    if not candidate:
        return fallback
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """Create and configure the Flask application."""
    load_dotenv(Path.cwd() / ".env")

    app = Flask(__name__, instance_relative_config=True)
    data_directory = Path(app.root_path).parent / "data"
    configured_secret_key = os.getenv("SECRET_KEY", "").strip()
    configured_secret_key_path = Path(
        str(
            (test_config or {}).get(
                "DASHBOARD_SECRET_KEY_FILE",
                data_directory / ".dashboard-secret-key",
            )
        )
    )
    if test_config and test_config.get("SECRET_KEY"):
        configured_secret_key = str(test_config["SECRET_KEY"])
    if not configured_secret_key:
        configured_secret_key = _load_or_create_secret_key(configured_secret_key_path)

    dashboard_hsts = _env_bool("DASHBOARD_HSTS", False)
    app.config.from_mapping(
        SECRET_KEY=configured_secret_key,
        DASHBOARD_SECRET_KEY_FILE=str(configured_secret_key_path),
        DASHBOARD_AUTH_USERNAME=os.getenv("DASHBOARD_AUTH_USERNAME", ""),
        DASHBOARD_AUTH_PASSWORD=os.getenv("DASHBOARD_AUTH_PASSWORD", ""),
        DASHBOARD_AUTH_REMEMBER_DAYS=_env_int("DASHBOARD_AUTH_REMEMBER_DAYS", 30),
        DASHBOARD_HSTS=dashboard_hsts,
        TRUSTED_HOSTS=_env_list("DASHBOARD_TRUSTED_HOSTS"),
        PERMANENT_SESSION_LIFETIME=timedelta(
            days=max(1, _env_int("DASHBOARD_AUTH_REMEMBER_DAYS", 30))
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_NAME="pizzeria_dashboard_session",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool(
            "DASHBOARD_SESSION_COOKIE_SECURE",
            True,
        ),
        SESSION_REFRESH_EACH_REQUEST=False,
        SERVICE_TIMEZONE=os.getenv("SERVICE_TIMEZONE", "America/New_York"),
        DATABASE_PATH=str(data_directory / "pizza_dashboard.db"),
        LEGACY_SERVICE_STATE_PATH=str(data_directory / "service_state.json"),
        PIZZA_CAPACITY_PER_WINDOW=_env_int("PIZZA_CAPACITY_PER_WINDOW", 3),
        ORDER_PREP_BUFFER_MINUTES=_env_int("ORDER_PREP_BUFFER_MINUTES", 20),
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
        SQUARE_SALAD_CATEGORY_NAMES=os.getenv(
            "SQUARE_SALAD_CATEGORY_NAMES", "Salad,Salads"
        ),
        SQUARE_SALAD_ITEM_KEYWORDS=os.getenv(
            "SQUARE_SALAD_ITEM_KEYWORDS", "salad"
        ),
        SQUARE_SIDE_CATEGORY_NAMES=os.getenv(
            "SQUARE_SIDE_CATEGORY_NAMES", "Side,Sides"
        ),
        SQUARE_SIDE_ITEM_KEYWORDS=os.getenv(
            "SQUARE_SIDE_ITEM_KEYWORDS", "side"
        ),
        SQUARE_DESSERT_CATEGORY_NAMES=os.getenv(
            "SQUARE_DESSERT_CATEGORY_NAMES", "Dessert,Desserts"
        ),
        SQUARE_DESSERT_ITEM_KEYWORDS=os.getenv(
            "SQUARE_DESSERT_ITEM_KEYWORDS", "dessert"
        ),
        SQUARE_MERCH_CATEGORY_NAMES=os.getenv(
            "SQUARE_MERCH_CATEGORY_NAMES", "Merch,Merchandise"
        ),
        SQUARE_MERCH_ITEM_KEYWORDS=os.getenv(
            "SQUARE_MERCH_ITEM_KEYWORDS", "merch,shirt,hat,tote"
        ),
        SQUARE_COOKIE_CATEGORY_NAMES=os.getenv(
            "SQUARE_COOKIE_CATEGORY_NAMES", "Cookie,Cookies"
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
    remember_days = max(1, int(app.config.get("DASHBOARD_AUTH_REMEMBER_DAYS", 30)))
    app.config["DASHBOARD_AUTH_REMEMBER_DAYS"] = remember_days
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=remember_days)

    signing_key = app.secret_key
    signing_key_bytes = signing_key if isinstance(signing_key, bytes) else str(signing_key).encode()
    auth_fingerprint = hmac.new(
        signing_key_bytes,
        f"{auth_username}\0{auth_password}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    def _is_authenticated() -> bool:
        saved_fingerprint = str(session.get("dashboard_auth_fingerprint", ""))
        return bool(saved_fingerprint) and secrets.compare_digest(
            saved_fingerprint,
            auth_fingerprint,
        )

    def _new_login_csrf_token() -> str:
        token = secrets.token_urlsafe(32)
        session["dashboard_login_csrf_token"] = token
        return token

    @app.route("/login", methods=("GET", "POST"))
    def dashboard_login():
        next_url = _safe_local_url(request.values.get("next"))
        if not auth_username or _is_authenticated():
            return redirect(next_url)

        error = ""
        status_code = 200
        if request.method == "POST":
            submitted_csrf_token = request.form.get("csrf_token", "")
            saved_csrf_token = str(session.get("dashboard_login_csrf_token", ""))
            csrf_valid = bool(saved_csrf_token) and secrets.compare_digest(
                submitted_csrf_token,
                saved_csrf_token,
            )
            supplied_username = request.form.get("username", "")
            supplied_password = request.form.get("password", "")
            credentials_valid = secrets.compare_digest(
                supplied_username,
                auth_username,
            ) and secrets.compare_digest(
                supplied_password,
                auth_password,
            )
            if csrf_valid and credentials_valid:
                session.clear()
                session.permanent = True
                session["dashboard_auth_fingerprint"] = auth_fingerprint
                return redirect(next_url, code=303)
            error = "The username or password was not recognized."
            status_code = 401

        return (
            render_template(
                "login.html",
                csrf_token=_new_login_csrf_token(),
                error=error,
                next_url=next_url,
                remember_days=remember_days,
            ),
            status_code,
        )

    @app.post("/logout")
    def dashboard_logout():
        session.clear()
        return redirect(url_for("dashboard_login"), code=303)

    @app.before_request
    def _require_dashboard_authentication():
        if request.path == "/healthz" or request.endpoint in {"dashboard_login", "static"}:
            return None
        if not auth_username:
            return None
        if _is_authenticated():
            return None
        login_url = url_for(
            "dashboard_login",
            next=_safe_local_url(request.full_path.rstrip("?")),
        )
        return redirect(login_url)

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
