from __future__ import annotations

from flask import Flask


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="development-only-change-me",
        SERVICE_TIMEZONE="America/New_York",
    )

    if test_config:
        app.config.update(test_config)

    from .dashboard import blueprint

    app.register_blueprint(blueprint)
    return app
