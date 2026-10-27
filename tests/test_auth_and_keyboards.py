from handlers.auth import normalize_phone, role_for_telegram_id
from keyboards import (
    main_menu_keyboard,
    photo_dialog_keyboard,
    request_navigation_keyboard,
)


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
        button.callback_data
        for row in main_menu_keyboard("admin", allow_role_switch=True).inline_keyboard
        for button in row
    }
    assert "menu:admins" in admin_buttons
    assert "view:master" in admin_buttons
    assert "menu:all_requests" not in admin_buttons

    master_buttons = {
        button.callback_data
        for row in main_menu_keyboard("master").inline_keyboard
        for button in row
    }
    assert "menu:all_requests" in master_buttons
    assert "menu:admins" not in master_buttons


def test_photo_dialog_has_skip_and_staff_photo_button():
    dialog_callbacks = {
        button.callback_data
        for row in photo_dialog_keyboard(main_menu_keyboard("client")).inline_keyboard
        for button in row
    }
    assert "ai:skip-photo" in dialog_callbacks

    keyboard = request_navigation_keyboard(
        view="staff",
        page=0,
        total_pages=1,
        menu=main_menu_keyboard("master"),
        request_id=12,
        current_status="new",
        has_photo=True,
    )
    assert "request:photo:12" in {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    }
