"""Opt-in Firefox test; uses sample orders in a temporary database, never Square."""

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


@pytest.mark.parametrize("days_ahead", [0, 3], ids=["today", "future"])
def test_scroll_past_active_timer(firefox, tmp_path, days_ahead):
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
    service_date = today + timedelta(days=days_ahead)

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
        page = context.new_page()
        page.goto(f"https://dashboard.test/?date={service_date}")
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
