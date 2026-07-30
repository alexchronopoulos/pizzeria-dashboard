from __future__ import annotations

# Support both of these launch styles:
#   python -m pizzeria_dashboard
#   python pizzeria_dashboard/__main__.py
# The second style does not normally establish package context, so add the
# project root to sys.path before importing the package.
if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pizzeria_dashboard import create_app


def main() -> None:
    """Run the local development server."""
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
