"""Hidden acceptance test for issues/01-delete-widget.md.

Imported the same way the target repo's own tests are: `from src.app import
app`. Run against a workspace with `src/` on the path (see this folder's
README.md), never inside examples/target-repo itself.
"""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_delete_removes_the_widget() -> None:
    created = client.post("/widgets", json={"name": "spanner", "price_cents": 500})
    widget_id = created.json()["id"]

    response = client.delete(f"/widgets/{widget_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/widgets/{widget_id}").status_code == 404
    ids = [widget["id"] for widget in client.get("/widgets").json()]
    assert widget_id not in ids


def test_delete_missing_widget_is_404() -> None:
    assert client.delete("/widgets/999999").status_code == 404


def test_delete_twice_is_404_the_second_time() -> None:
    created = client.post("/widgets", json={"name": "rivet", "price_cents": 10})
    widget_id = created.json()["id"]

    first = client.delete(f"/widgets/{widget_id}")
    second = client.delete(f"/widgets/{widget_id}")

    assert first.status_code == 204
    assert second.status_code == 404
