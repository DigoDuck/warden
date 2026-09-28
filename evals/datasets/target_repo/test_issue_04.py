"""Hidden acceptance test for issues/04-reject-blank-names.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_whitespace_only_name_is_rejected() -> None:
    response = client.post("/widgets", json={"name": "   ", "price_cents": 100})
    assert response.status_code == 422


def test_empty_name_is_rejected() -> None:
    response = client.post("/widgets", json={"name": "", "price_cents": 100})
    assert response.status_code == 422


def test_name_with_incidental_whitespace_is_trimmed_and_accepted() -> None:
    response = client.post("/widgets", json={"name": "  bolt-cutter  ", "price_cents": 100})
    assert response.status_code == 201
    assert response.json()["name"] == "bolt-cutter"


def test_single_character_name_still_works() -> None:
    response = client.post("/widgets", json={"name": "a", "price_cents": 1})
    assert response.status_code == 201
