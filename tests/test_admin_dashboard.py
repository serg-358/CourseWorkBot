from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from repair_bot.services.admin_dashboard import (
    build_dashboard_data,
    dashboard_detail_message,
    build_dashboard_xlsx,
    dashboard_message,
    moscow_datetime,
)


def test_dashboard_data_filters_period_and_exports_xlsx():
    requests = [
        SimpleNamespace(status="new", priority="low", created_at=datetime(2026, 11, 20)),
        SimpleNamespace(status="completed", priority="high", created_at=datetime(2026, 11, 22)),
    ]
    ratings = [SimpleNamespace(score=5, created_at=datetime(2026, 11, 22))]
    complaints = [SimpleNamespace(category="quality", created_at=datetime(2026, 11, 22))]

    dashboard = build_dashboard_data(requests, ratings, complaints, "7")
    workbook = load_workbook(BytesIO(build_dashboard_xlsx(dashboard, extras={"completed_cost": 125_000, "average_check": 125_000, "top_clients": [("Иван", 125_000)]})))

    assert dashboard.request_count == 2
    assert dashboard.status_counts == {
        "new": 1,
        "in_progress": 0,
        "completed": 1,
        "cancelled": 0,
    }
    assert dashboard.average_rating == 5
    assert dashboard.complaint_counts["quality"] == 1
    assert dashboard.timeline_labels == ["16.11", "17.11", "18.11", "19.11", "20.11", "21.11", "22.11"]
    assert dashboard.timeline_counts == [0, 0, 0, 0, 1, 0, 1]
    assert workbook.sheetnames == [
        "Сводка",
        "Заявки по интервалам",
        "Статусы",
        "Срочность",
        "Оценки",
        "Жалобы",
        "Клиенты по стоимости",
        "Мастера",
    ]
    assert workbook["Сводка"]["B6"].value == "1 250 ₽"
    assert workbook["Сводка"]["B6"].alignment.horizontal == "right"
    assert list(workbook["Клиенты по стоимости"].values)[1] == ("Иван", "1 250 ₽")
    assert workbook["Клиенты по стоимости"]["B2"].alignment.horizontal == "right"
    assert workbook["Оценки"]["A2"].alignment.horizontal == "right"


def test_dashboard_groups_long_period_into_at_most_seven_real_intervals():
    requests = [
        SimpleNamespace(status="new", priority="low", created_at=datetime(2026, 10, 1)),
        SimpleNamespace(status="completed", priority="high", created_at=datetime(2026, 11, 22)),
    ]

    dashboard = build_dashboard_data(requests, [], [], "all")

    assert len(dashboard.timeline_labels) <= 7
    assert sum(dashboard.timeline_counts) == 2


def test_dashboard_uses_moscow_dates_and_includes_trash():
    requests = [
        SimpleNamespace(
            status="new",
            priority="low",
            created_at=datetime(2026, 11, 21, 21, 30),
            lifecycle=None,
        ),
        SimpleNamespace(
            status="completed",
            priority="high",
            created_at=datetime(2026, 11, 22, 12),
            lifecycle=SimpleNamespace(deleted_at=datetime(2026, 11, 22, 13)),
        ),
    ]

    dashboard = build_dashboard_data(requests, [], [], "7")

    assert dashboard.period_end.isoformat() == "2026-11-22"
    assert dashboard.timeline_counts[-1] == 2
    assert moscow_datetime(datetime(2026, 11, 22, 21, 30)).strftime("%d.%m.%Y %H:%M") == "23.11.2026 00:30"
    assert dashboard.trash_count == 1


def test_dashboard_messages_split_summary_and_details():
    dashboard = build_dashboard_data([], [], [], "7")

    message = dashboard_message(dashboard)
    details = dashboard_detail_message(
        "cost",
        dashboard,
        revenue_kopecks=125_000,
        average_check_kopecks=125_000,
        cancellation_rate=20,
        feedback_rate=50,
        average_completion_duration=90 * 60,
        top_clients=[("Иван", 125_000)],
        master_rows=[("Мастер", 1, 125_000, 5.0, 0)],
    )

    assert "Заявки" in message
    assert "Стоимость завершённых ремонтов" in details
    assert "Клиенты по стоимости" in details
    assert "1 ч. 30 мин." in details
