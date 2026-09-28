"""Hidden acceptance test for issues/05-filter-widgets-by-min-price.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_no_min_price_returns_everything_unfiltered() -> None:
    response = client.get("/widgets")
    assert response.status_code == 200
    names = {widget["name"] for widget in response.json()}
    assert {"bolt", "washer"} <= names


def test_min_price_filters_out_cheaper_widgets() -> None:
    response = client.get("/widgets", params={"min_price": 200})
    assert response.status_code == 200
    prices = [widget["price_cents"] for widget in response.json()]
    assert all(price >= 200 for price in prices)
    assert 250 in prices  # bolt
    assert 80 not in prices  # washer, filtered out


def test_min_price_zero_returns_everything() -> None:
    response = client.get("/widgets", params={"min_price": 0})
    assert response.status_code == 200
    assert len(response.json()) >= 2


def test_negative_min_price_is_a_client_error() -> None:
    response = client.get("/widgets", params={"min_price": -1})
    assert response.status_code == 422


def test_non_integer_min_price_is_still_a_client_error() -> None:
    response = client.get("/widgets", params={"min_price": "abc"})
    assert response.status_code == 422
