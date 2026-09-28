"""Hidden acceptance test for issues/06-consistent-validation-errors.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_validation_error_detail_is_a_plain_string() -> None:
    response = client.post("/widgets", json={"name": "x", "price_cents": -1})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "price_cents" in detail


def test_not_found_error_detail_is_still_a_plain_string() -> None:
    response = client.get("/widgets/999999")

    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_status_codes_are_unchanged() -> None:
    assert client.get("/widgets/1").status_code == 200
    assert client.get("/widgets/999999").status_code == 404
    assert client.post("/widgets", json={"name": "x", "price_cents": -1}).status_code == 422
