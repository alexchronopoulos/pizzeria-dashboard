from pizzeria_dashboard import create_app


def test_dashboard_renders_sample_service() -> None:
    app = create_app({"TESTING": True})
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Pizzeria Mari" in response.data
    assert b"Production board" in response.data
    assert b"Tomato Pie" in response.data
    assert b"Release candidate" in response.data


def test_sync_redirects_to_dashboard() -> None:
    app = create_app({"TESTING": True})
    client = app.test_client()

    response = client.post("/sync")

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/")


def test_health_endpoint() -> None:
    app = create_app({"TESTING": True})
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
