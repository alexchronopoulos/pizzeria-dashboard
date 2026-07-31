# Pizzeria Mari Production Dashboard

A local, single-user production board for Pizzeria Mari. Square remains the
system of record; this application pulls pickup orders into SQLite, groups them
by service time, calculates pizza load, tracks prep inventory, and will later
release capacity by marking eligible Square orders as picked up.

## Sprint 3.8

Sprint 3.8 extends completed, unscheduled counter orders to the production board, automatically assigns times written at the beginning or end of a Square Ticket Name, and lets the kitchen override the pickup slot without modifying the original Square order.

- Pulls scheduled pickup orders and completed unscheduled counter sales for the date selected in the dashboard.
- Keeps historical dates in SQLite for later review.
- Uses each fulfillment's `pickup_at` timestamp—not the order creation date—to
  decide which service date an order belongs to.
- Searches a configurable lookback window so preorders placed before the service
  date are included.
- Follows Square pagination automatically.
- Reads item categories from the Square Catalog when available.
- Preserves pizza modifiers using Square kitchen names.
- Counts named salad and `Side` modifiers against configurable prepared inventory.
- Highlights cookie, side, salad, and removal modifiers for fast kitchen scanning.
- Resolves receipt and payment details for the order inspector without cluttering production cards.
- Hides drink-category items and individual slice line items from the production board. Mixed pie-and-slice walk-ins remain visible, with only production-relevant items shown.
- Retains Square order version and fulfillment identifiers needed for the future
  Release Capacity action.
- Opens every order in a modal with the cached kitchen document, the complete live Square order, associated payment documents, fulfillment recipients, source fields, and identifiers useful for debugging `Guest` records.
- Redacts stable card fingerprints and credential-like fields from the debug JSON.
- Adds an independent eight-minute timer to every pizza line item. Timers can be started, paused, resumed, and reset.
- Stores active timer state in the browser so countdowns survive a page refresh on the same device.
- Detects completed orders with no actual pickup timestamp as walk-ins when their local `created_at` or `closed_at` date matches the selected service date.
- Shows new walk-ins in an **Unscheduled** lane using Square ticket names when available.
- Parses configured pickup times from Ticket Names such as `Sam 7:30` or `5:45 Peter` and automatically places the walk-in into the matching service slot.
- Supports dragging walk-ins into configured service slots or back to the Unscheduled lane.
- Adds a pickup-slot selector to the order-details modal for quick reassignment when the destination slot is far down the page.
- Stores manual walk-in slot assignments and explicit Unscheduled overrides in SQLite so a Square refresh preserves them.
- Does not write timer or walk-in assignment state back to Square.

The active database remains:

```text
data/pizza_dashboard.db
```

## Project principles

- Keep the dashboard focused on production during service.
- Prefer obvious code over abstraction.
- Poll Square rather than adding webhooks unless polling proves insufficient.
- Keep Square as the source of truth.
- Use SQLite as a local cache plus application metadata store.
- Cache source orders as documents so the model can evolve without frequent
  schema migrations.
- Use server-rendered HTML first; add HTMX only when it materially improves an action.
- Add complexity only when an observed workflow requires it.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- A Square Developer application connected to the Pizzeria Mari Square account

## Configure Square

For this single-account internal integration, use the production personal access
token from the Square Developer Console. Never commit it to Git.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```dotenv
ORDER_SOURCE=square
SQUARE_ACCESS_TOKEN=your_production_personal_access_token
SQUARE_LOCATION_ID=
```

If the account has only one active location, the application selects it
automatically. To verify the token and list location IDs:

```bash
uv sync
uv run pizzeria-square-check
```

When more than one active location exists, copy the correct ID into
`SQUARE_LOCATION_ID`.

The default API version is `2026-07-15`. The token needs access to Orders,
Catalog Items, and Merchant Profile data. A personal access token for your own
Square account has unrestricted account access, so protect it carefully.

## Item classification

The dashboard reads Square catalog categories to decide which line items are
pizzas or drinks. Match these values to your Square item library categories:

```dotenv
SQUARE_PIZZA_CATEGORY_NAMES=Pizza,Pizzas
SQUARE_HIDDEN_CATEGORY_NAMES=Drink,Drinks,Beverage,Beverages
```

Name-based fallbacks are used if an order contains an ad hoc item or catalog
details cannot be loaded:

```dotenv
SQUARE_PIZZA_ITEM_KEYWORDS=pizza,pie
SQUARE_SLICE_CATEGORY_NAMES=Slice,Slices
SQUARE_SLICE_ITEM_KEYWORDS=slice,slices
SQUARE_HIDDEN_ITEM_KEYWORDS=drink,beverage,coke,soda,water
SQUARE_SALAD_MODIFIER_KEYWORDS=salad
SQUARE_SIDE_MODIFIER_KEYWORDS=side
SQUARE_COOKIE_MODIFIER_KEYWORDS=cookie
```

Items in a Slice category, or whose names contain the configured slice keywords, remain in the cached Square document but are omitted from the production board. This allows a mixed whole-pie and slice walk-in to display without showing the slice line. A modifier whose name contains `salad` is treated as a named salad. Modifiers containing the word `Side` are tracked separately, and cookie modifiers trigger a cookie alert. Prepared dough, salad, and side counts are edited inside **Service setup** and summarized in read-only cards during service.

## How date sync works

Square's Search Orders endpoint can filter on order lifecycle timestamps such as
`created_at`, but not directly on the scheduled pickup time. The dashboard:

1. Searches `OPEN` and `COMPLETED` orders created during the configured lookback period.
2. Follows every result page.
3. Keeps scheduled pickup fulfillments whose local `pickup_at` date matches the selected service date.
4. Separately keeps completed orders with no actual pickup timestamp when either their local `closed_at` or `created_at` date matches the selected service date. Source labels, receipts, tenders, and payment metadata are not required.
5. Atomically replaces that date's SQLite snapshot after a successful pull while retaining valid local walk-in slot assignments.

The default lookback is 60 days:

```dotenv
SQUARE_ORDER_LOOKBACK_DAYS=60
```

Your orders are normally placed only hours ahead, so this is intentionally
conservative. Increase it if you ever accept preorders more than 60 days early.

## Run it

```bash
uv sync
uv run pytest
uv run pizzeria-dashboard
```

Alternative launch commands:

```bash
uv run python run.py
uv run python -m pizzeria_dashboard
```

Open `http://localhost:5000`, select a service date, and press **Pull from
Square**. Click any order card to open the Square order inspector. The modal
loads the current order and associated tender payments directly from Square,
while retaining a cached fallback if the live request fails. Each pizza row also includes an eight-minute bake timer. Timer state is local to that browser/device and survives normal page refreshes. Completed orders without a pickup timestamp first appear in the Unscheduled walk-in lane unless a configured slot can be parsed from the beginning or end of the Ticket Name. Drag them onto a service slot or use the pickup selector in the order-details modal. Manual choices override Ticket Name parsing and survive normal Square refreshes.

If Square is temporarily unavailable, previously cached dates remain visible.
A failed pull does not erase the existing cache.

## Sample mode

To work on the UI without using Square:

```dotenv
ORDER_SOURCE=sample
```

`ORDER_SOURCE=auto` uses Square when a token is configured and sample data
otherwise.

## Inspect the database

```bash
sqlite3 data/pizza_dashboard.db '.tables'
sqlite3 data/pizza_dashboard.db \
  'select service_date, source, order_count, synced_at from sync_runs order by service_date;'
```

To reset runtime data:

```bash
rm -f data/pizza_dashboard.db data/pizza_dashboard.db-shm data/pizza_dashboard.db-wal
```

The app recreates the schema on the next start.

## Current structure

```text
pizza-dashboard/
├── app.py
├── run.py
├── pyproject.toml
├── .env.example
├── pizzeria_dashboard/
│   ├── __init__.py
│   ├── dashboard.py
│   ├── database.py
│   ├── domain.py
│   ├── sample_data.py
│   ├── service_state.py
│   ├── square_api.py
│   ├── square_cli.py
│   ├── square_orders.py
│   ├── sync_service.py
│   ├── static/dashboard.js
│   ├── static/style.css
│   └── templates/
├── tests/
└── data/
```

## Sprint sequence

1. **Sprint 1 — complete:** production board with realistic sample orders.
2. **Sprint 2 — complete:** SQLite order cache and production metadata.
3. **Sprint 3 — complete:** pull pickup orders from Square for a selected date.
4. **Sprint 4:** review and override item/modifier classification from the dashboard.
5. **Sprint 5:** manually release capacity from the dashboard.
6. **Sprint 6:** guarded automatic release rules.

### Kitchen names and category matching

The Square sync prefers `CatalogItem.kitchen_name` for pizza names and
`CatalogModifier.kitchen_name` for modifier names. Customer-facing names are
used only when a kitchen name is absent.

Pizza category values are comma-separated and may contain spaces, for example:

```dotenv
SQUARE_PIZZA_CATEGORY_NAMES=Traditional Pies,Mari Pies,Seasonal Special Pies
```

Category names containing the configured pizza keywords (`pizza` or `pie` by
default) are also recognized, so descriptive names such as `Traditional Pies`
work without an exact-match entry.

Completed pickup records without a recipient name are omitted from the
production board. Active orders without a name remain visible as `Guest`, and
named completed fulfillments remain visible because they may represent slots
manually released through Square.


## Receipt and payment details

Square payment data is retained for the order inspector but is no longer displayed on production cards. If the Payments API cannot be read, order sync continues and shows a warning.
