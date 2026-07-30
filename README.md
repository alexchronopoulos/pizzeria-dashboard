# Pizzeria Mari Production Dashboard

A local, single-user production board for Pizzeria Mari. Square remains the
system of record; this application will pull orders, group them into pickup
windows, calculate pizza load, and eventually release capacity by marking
eligible Square orders as picked up.

This sprint still uses sample orders, but the production behavior is now modeled
more closely:

- two or three customer orders can appear in each 15-minute pickup slot;
- every slot prominently displays its total pizza count;
- drinks are hidden;
- pizza modifiers stay attached to their pizza line;
- side salads are represented as named modifiers, not standalone orders;
- salad orders receive a solid visual highlight;
- release candidates appear only for unreleased one-pie orders in slots with
  fewer than three pizzas;
- the release card lists the pickup times that have releasable capacity;
- a service-date picker plus previous/next-day controls lets the operator view one date at a time, including past dates;
- dough-ball and salad-prep quantities can be entered and are persisted separately for each service date in `data/service_state.json`;
- the board calculates dough balls and each salad type remaining after current
  orders.

There are no Square credentials, SQLite files, or Square API calls yet. The selected date is already carried through the routes so the future Square adapter can request the matching daily time range.

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

Run these commands from the repository root. Do not execute individual files
such as `pizzeria_dashboard/dashboard.py`.

Open `http://localhost:5000`.

The development server listens on all interfaces, so another device on your
local network can reach it at `http://<server-ip>:5000` if your firewall permits
that port.

## Run tests

```bash
uv run pytest
```

## Runtime inventory state

The service-prep inputs are stored in:

```text
data/service_state.json
```

That file is ignored by Git. It stores a separate prep state for every selected service date. Deleting it restores the sample defaults the next time the dashboard loads.

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
│   ├── service_state.py
│   ├── static/style.css
│   └── templates/
├── tests/
└── data/
```

## Sprint sequence

1. **Sprint 1 — complete:** working production board with realistic sample orders.
2. **Sprint 2:** SQLite cache and production metadata.
3. **Sprint 3:** pull pickup orders from Square for the selected service date.
4. **Sprint 4:** identify pizza catalog items and calculate production units.
5. **Sprint 5:** manually release capacity from the dashboard.
6. **Sprint 6:** guarded automatic release rules.

## Notes

The dashboard has no browser-side dependencies and works without public CDNs.
