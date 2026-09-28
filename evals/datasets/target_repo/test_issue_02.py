"""Hidden acceptance test for issues/02-patch-widget.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_patch_updates_only_the_given_fields() -> None:
    created = client.post("/widgets", json={"name": "gear", "price_cents": 100})
    widget_id = created.json()["id"]

    response = client.patch(f"/widgets/{widget_id}", json={"price_cents": 150})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == widget_id
    assert body["name"] == "gear"
    assert body["price_cents"] == 150


def test_patch_with_empty_body_is_a_noop() -> None:
    created = client.post("/widgets", json={"name": "cog", "price_cents": 42})
    widget_id = created.json()["id"]

    response = client.patch(f"/widgets/{widget_id}", json={})

    assert response.status_code == 200
    assert response.json() == {"id": widget_id, "name": "cog", "price_cents": 42}


def test_patch_rejects_invalid_price_and_keeps_the_widget_unchanged() -> None:
    created = client.post("/widgets", json={"name": "sprocket", "price_cents": 10})
    widget_id = created.json()["id"]

    response = client.patch(f"/widgets/{widget_id}", json={"price_cents": -5})

    assert response.status_code == 422
    assert client.get(f"/widgets/{widget_id}").json()["price_cents"] == 10


def test_patch_rejects_an_empty_name_and_keeps_the_widget_unchanged() -> None:
    created = client.post("/widgets", json={"name": "flange", "price_cents": 10})
    widget_id = created.json()["id"]

    response = client.patch(f"/widgets/{widget_id}", json={"name": ""})

    assert response.status_code == 422
    assert client.get(f"/widgets/{widget_id}").json()["name"] == "flange"


def test_patch_missing_widget_is_404() -> None:
    assert client.patch("/widgets/999999", json={"name": "x"}).status_code == 404
