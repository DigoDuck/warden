"""Hidden acceptance test for issues/09-fix-case-insensitive-search.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_search_matches_regardless_of_case() -> None:
    for query in ("BOLT", "bolt", "Bolt", "bOlT"):
        response = client.get("/widgets/search", params={"q": query})
        assert response.status_code == 200
        names = [widget["name"] for widget in response.json()]
        assert "bolt" in names, f"query {query!r} should match widget 'bolt'"


def test_search_matches_substring_across_case_too() -> None:
    client.post("/widgets", json={"name": "Steel Bolt", "price_cents": 400})

    response = client.get("/widgets/search", params={"q": "bolt"})

    names = [widget["name"] for widget in response.json()]
    assert "Steel Bolt" in names


def test_empty_query_still_returns_everything() -> None:
    response = client.get("/widgets/search")
    assert response.status_code == 200
    assert len(response.json()) >= 2


def test_no_match_returns_an_empty_list_not_an_error() -> None:
    response = client.get("/widgets/search", params={"q": "definitely-not-a-widget-name"})
    assert response.status_code == 200
    assert response.json() == []
