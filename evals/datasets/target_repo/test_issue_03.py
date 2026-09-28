"""Hidden acceptance test for issues/03-reject-duplicate-names.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_duplicate_name_is_rejected_case_insensitively() -> None:
    first = client.post("/widgets", json={"name": "unique-thingamajig", "price_cents": 10})
    assert first.status_code == 201

    response = client.post("/widgets", json={"name": "Unique-Thingamajig", "price_cents": 20})

    assert response.status_code == 409
    assert "detail" in response.json()


def test_new_name_still_creates() -> None:
    response = client.post("/widgets", json={"name": "totally-new-name", "price_cents": 5})
    assert response.status_code == 201


def test_seeded_names_already_conflict() -> None:
    # "bolt" is one of the two seed widgets; this needs no setup of its own.
    response = client.post("/widgets", json={"name": "BOLT", "price_cents": 999})
    assert response.status_code == 409
