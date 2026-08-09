"""Convenient local entry point for the production dashboard."""

from pizzeria_dashboard import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
