from datetime import datetime
from types import SimpleNamespace

from repair_bot.services.admin_dashboard import build_dashboard_data, render_dashboard_chart


def test_dashboard_data_filters_period_and_renders_png():
    requests = [
        SimpleNamespace(status="new", priority="low", created_at=datetime(2026, 11, 20)),
        SimpleNamespace(status="completed", priority="high", created_at=datetime(2026, 11, 22)),
    ]
    ratings = [SimpleNamespace(score=5, created_at=datetime(2026, 11, 22))]
    complaints = [SimpleNamespace(category="quality", created_at=datetime(2026, 11, 22))]

    dashboard = build_dashboard_data(requests, ratings, complaints, "7")
    image = render_dashboard_chart(dashboard)

    assert dashboard.request_count == 2
    assert dashboard.status_counts == {
        "new": 1,
        "in_progress": 0,
        "completed": 1,
        "cancelled": 0,
    }
    assert dashboard.average_rating == 5
    assert dashboard.complaint_counts["quality"] == 1
    assert image.startswith(b"\x89PNG")
