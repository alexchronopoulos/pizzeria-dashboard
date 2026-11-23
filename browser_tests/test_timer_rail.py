"""Opt-in board layout checks in Firefox; sample orders only, never Square."""

from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect, sync_playwright

from pizzeria_dashboard import create_app


@pytest.fixture(scope="module")
def firefox():
    with sync_playwright() as playwright, playwright.firefox.launch() as browser:
        yield browser


@pytest.fixture
def sample_board(firefox, tmp_path):
    app = create_app({
        "TESTING": True, "ORDER_SOURCE": "sample", "AUTO_SEED_SAMPLE_DATA": True,
        "DATABASE_PATH": str(tmp_path / "sample.db"),
        "LEGACY_SERVICE_STATE_PATH": str(tmp_path / "service_state.json"),
        "SECRET_KEY": "browser-test-only", "SQUARE_ACCESS_TOKEN": "",
        "DASHBOARD_AUTH_USERNAME": "", "DASHBOARD_AUTH_PASSWORD": "",
        "TRUSTED_HOSTS": ["dashboard.test"],
    })
    client = app.test_client()
    today = datetime.now(ZoneInfo(app.config["SERVICE_TIMEZONE"])).date()

    # Send browser requests (including Start and polling) to the real Flask
    # handlers without needing an HTTP listener or a running service dashboard.
    def serve(route):
        req = route.request
        url = urlsplit(req.url)
        if url.netloc != "dashboard.test":
            route.abort()
            return
        response = client.open(
            url.path, query_string=url.query, method=req.method,
            base_url="https://dashboard.test", data=req.post_data_buffer,
            content_type=req.headers.get("content-type"),
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    with firefox.new_context(viewport={"width": 1180, "height": 820}) as context:
        context.route("**/*", serve)
        yield context.new_page(), today


@pytest.mark.parametrize("days_ahead", [0, 3], ids=["today", "future"])
def test_scroll_past_active_timer(sample_board, days_ahead):
    page, today = sample_board
    page.goto(f"https://dashboard.test/?date={today + timedelta(days=days_ahead)}")
    timer = page.locator("[data-bake-timer]").first
    rail = page.locator("[data-active-timer-rail]")
    alert = rail.get_by_role("button")
    timer.evaluate("el => el.scrollIntoView({block: 'center'})")
    timer.locator("[data-bake-timer-toggle]").click()
    expect(timer).to_have_attribute("data-timer-status", "running")
    deadline = timer.get_attribute("data-timer-end-at-ms")
    expect(rail).to_be_hidden()

    page.mouse.wheel(0, 1600)
    expect(timer).not_to_be_in_viewport()
    expect(alert).to_be_in_viewport(ratio=1)
    assert alert.bounding_box()["x"] > 1180 / 2
    alert.click()
    expect(timer).to_be_in_viewport(ratio=1)
    expect(rail).to_be_hidden()
    expect(timer).to_have_attribute("data-timer-end-at-ms", deadline)


@pytest.mark.parametrize("width,height", [(1180, 820), (1194, 834), (390, 844)])
def test_order_action_row_before_and_after_marking(sample_board, width, height):
    page, today = sample_board
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"https://dashboard.test/?date={today + timedelta(days=1)}")
    # Two pies: only one action row, with the timer stack free to wrap separately.
    card = page.locator('.order-row[data-order-id$="PM-1050"]')
    buttons = card.locator(".order-action-row > button")
    expect(buttons).to_have_count(3)
    boxed, arrived, picked_up = buttons.all()
    for button, label in zip((boxed, arrived, picked_up), ("Mark Boxed", "Mark Arrived", "Mark Picked Up"), strict=True):
        expect(button).to_contain_text(label)

    def check_geometry():
        cells = [button.bounding_box() for button in (boxed, arrived, picked_up)]
        assert all(cell and cell["height"] >= 44 for cell in cells)
        assert max(c["y"] for c in cells) - min(c["y"] for c in cells) < 1
        assert max(c["width"] for c in cells) - min(c["width"] for c in cells) < 1
        assert cells[0]["x"] + cells[0]["width"] <= cells[1]["x"]
        assert cells[1]["x"] + cells[1]["width"] <= cells[2]["x"]
        bounds = card.bounding_box()
        assert cells[0]["x"] >= bounds["x"] and cells[2]["x"] + cells[2]["width"] <= bounds["x"] + bounds["width"]
        assert buttons.evaluate_all("els => els.every(e => e.scrollWidth <= e.clientWidth + 1 && e.scrollHeight <= e.clientHeight + 1)")
        return cells[0]["height"]

    initial_height = check_geometry()
    for button in (boxed, arrived, picked_up):
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
    marked_height = check_geometry()
    if width > 1000:
        assert marked_height == initial_height
    for button in (arrived, picked_up):
        expect(button.locator("time")).not_to_be_empty()
