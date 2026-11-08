from repair_bot.handlers.auth import normalize_phone, role_for_telegram_id
from repair_bot.keyboards import (
    main_menu_keyboard,
    photo_dialog_keyboard,
    request_navigation_keyboard,
    urgency_keyboard,
)
from repair_bot.handlers.staff_requests import parse_client_price


def test_normalize_russian_phone_formats():
    assert normalize_phone("89991234567") == "+79991234567"
    assert normalize_phone("+7 (999) 123-45-67") == "+79991234567"
    assert normalize_phone("9991234567") == "+79991234567"
    assert normalize_phone("8abc9991234567") is None


def test_roles_come_from_separate_allowlists(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "101, 103")
    monkeypatch.setenv("MASTER_TELEGRAM_IDS", "102, 103")

    assert role_for_telegram_id(101) == "admin"
    assert role_for_telegram_id(102) == "master"
    # При совпадении в списках приоритет остаётся у администратора.
    assert role_for_telegram_id(103) == "admin"
    assert role_for_telegram_id(104) == "client"


def test_admin_and_master_have_separate_menus():
    admin_buttons = {
        button.callback_data: button.text
        for row in main_menu_keyboard("admin", allow_role_switch=True).inline_keyboard
        for button in row
    }
    assert "menu:admins" in admin_buttons
    assert "view:master" in admin_buttons
    assert "menu:all_requests" not in admin_buttons
    assert admin_buttons["menu:admins"] == "🛡 Администраторы"
    assert admin_buttons["view:master"] == "🛠 Открыть панель мастера"

    master_buttons = {
        button.callback_data: button.text
        for row in main_menu_keyboard("master").inline_keyboard
        for button in row
    }
    assert "menu:all_requests" in master_buttons
    assert "menu:admins" not in master_buttons
    assert master_buttons["menu:all_requests"] == "🧾 Заявки на ремонт"


def test_photo_dialog_has_skip_and_staff_photo_button():
    dialog_callbacks = {
        button.callback_data
        for row in photo_dialog_keyboard(main_menu_keyboard("client")).inline_keyboard
        for button in row
    }
    assert "ai:skip-photo" in dialog_callbacks
    assert next(
        button.text
        for row in photo_dialog_keyboard(main_menu_keyboard("client")).inline_keyboard
        for button in row
        if button.callback_data == "ai:skip-photo"
    ) == "🖼️ Пропустить фото"

    keyboard = request_navigation_keyboard(
        view="staff",
        page=0,
        total_pages=1,
        menu=main_menu_keyboard("master"),
        request_id=12,
        current_status="new",
        has_photo=True,
        can_edit_assessment=True,
    )
    assert "request:photo:12" in {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
    assert "request:edit:12" in {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }


def test_client_price_is_parsed_as_integer_kopecks():
    assert parse_client_price("2500") == 250000
    assert parse_client_price("2500,50") == 250050
    assert parse_client_price("0") == 0
    assert parse_client_price("2 500") == 250000
    assert parse_client_price("2500.555") is None


def test_urgency_keyboard_shows_selected_fee_separately():
    from types import SimpleNamespace

    keyboard = urgency_keyboard(
        [
            SimpleNamespace(code="ordinary", label="Обычная", surcharge_kopecks=0),
            SimpleNamespace(code="urgent", label="Срочная", surcharge_kopecks=50_000),
            SimpleNamespace(code="critical", label="Критическая", surcharge_kopecks=100_000),
        ]
    )
    buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data != "dialog:cancel"
    ]
    assert [button.text for button in buttons] == [
        "Обычная · без доплаты",
        "Срочная · +500 ₽",
        "Критическая · +1 000 ₽",
    ]
    assert [button.callback_data for button in buttons] == [
        "urgency:choose:ordinary",
        "urgency:choose:urgent",
        "urgency:choose:critical",
    ]
