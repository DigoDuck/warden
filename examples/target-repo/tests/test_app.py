from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_health_reports_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_widgets_returns_the_seeded_ones() -> None:
    response = client.get("/widgets")
    assert response.status_code == 200
    names = [widget["name"] for widget in response.json()]
    assert "bolt" in names


def test_get_widget_returns_one() -> None:
    response = client.get("/widgets/1")
    assert response.status_code == 200
    assert response.json()["name"] == "bolt"


def test_get_missing_widget_is_404() -> None:
    assert client.get("/widgets/999").status_code == 404


def test_create_widget_rejects_a_negative_price() -> None:
    response = client.post("/widgets", json={"name": "nut", "price_cents": -1})
    assert response.status_code == 422
