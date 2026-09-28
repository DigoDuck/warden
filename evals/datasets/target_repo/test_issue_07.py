"""Hidden acceptance test for issues/07-widget-store-reset.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_reset_restores_the_two_seed_widgets() -> None:
    client.post("/widgets", json={"name": "extra", "price_cents": 999})
    client.delete("/widgets/1")

    response = client.post("/widgets/reset")

    assert response.status_code == 204
    widgets = client.get("/widgets").json()
    by_name = {w["name"]: w for w in widgets}
    assert len(widgets) == 2
    assert by_name["bolt"] == {"id": 1, "name": "bolt", "price_cents": 250}
    assert by_name["washer"] == {"id": 2, "name": "washer", "price_cents": 80}


def test_next_created_widget_after_reset_gets_id_three() -> None:
    client.post("/widgets", json={"name": "throwaway-a", "price_cents": 1})
    client.post("/widgets", json={"name": "throwaway-b", "price_cents": 1})

    client.post("/widgets/reset")
    created = client.post("/widgets", json={"name": "fresh", "price_cents": 5})

    assert created.json()["id"] == 3


def test_reset_is_idempotent_and_safe_to_call_twice() -> None:
    client.post("/widgets", json={"name": "one-more", "price_cents": 1})

    first = client.post("/widgets/reset")
    second = client.post("/widgets/reset")

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(client.get("/widgets").json()) == 2
