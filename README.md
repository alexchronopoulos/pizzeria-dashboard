# Pizzeria Mari Production Dashboard

A local, single-user production board for Pizzeria Mari. Square will remain the
system of record; this application will pull orders, group them into pickup
windows, calculate pizza load, and eventually release capacity by marking
eligible Square orders as picked up.

This first sprint deliberately uses sample data. There are no credentials,
database files, or Square API calls yet.

## Project principles

- Keep the dashboard focused on production during service.
- Prefer obvious code over abstraction.
- Poll Square rather than adding webhooks unless polling proves insufficient.
- Keep Square as the source of truth.
- Use SQLite as a local cache plus application metadata store.
- Use server-rendered HTML first; add HTMX only when it materially improves an action.
- Add complexity only when an observed workflow requires it.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)

## Run it

```bash
uv sync
uv run pizzeria-dashboard
```

Alternative launch commands:

```bash
uv run python run.py
uv run python -m pizzeria_dashboard
```

Run these commands from the repository root. Do not execute individual files such as `pizzeria_dashboard/dashboard.py`.

Open `http://localhost:5000`.

The development server listens on all interfaces, so another device on your
local network can reach it at `http://<server-ip>:5000` if your firewall permits
that port.

## Run tests

```bash
uv run pytest
```

## Current structure

```text
pizza-dashboard/
├── app.py
├── run.py
├── pyproject.toml
├── pizzeria_dashboard/
│   ├── __init__.py
│   ├── dashboard.py
│   ├── sample_data.py
│   ├── static/style.css
│   └── templates/
├── tests/
└── data/
```

## Sprint sequence

1. **Sprint 1 — complete:** working production board with sample orders.
2. **Sprint 2:** SQLite cache and production metadata.
3. **Sprint 3:** pull today's pickup orders from Square.
4. **Sprint 4:** identify pizza catalog items and calculate production units.
5. **Sprint 5:** manually release capacity from the dashboard.
6. **Sprint 6:** guarded automatic release rules.

## Notes

The current dashboard has no browser-side dependencies and works without public CDNs.
