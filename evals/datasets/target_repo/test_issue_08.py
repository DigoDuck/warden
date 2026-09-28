"""Hidden acceptance test for issues/08-fix-widget-stats-average.md."""

from fastapi.testclient import TestClient

from src.app import app

client = TestClient(app)


def test_stats_reports_the_mean_of_the_two_seed_widgets() -> None:
    response = client.get("/widgets/stats")
    assert response.status_code == 200
    assert response.json()["average_price_cents"] == 165  # (250 + 80) / 2


def test_stats_updates_after_a_new_widget_is_created() -> None:
    client.post("/widgets", json={"name": "expensive-thing", "price_cents": 300})

    response = client.get("/widgets/stats")

    assert response.json()["average_price_cents"] == 210  # (250 + 80 + 300) / 3
