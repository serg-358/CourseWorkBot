from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe

from handlers import auth
from keyboards import main_menu_keyboard, request_navigation_keyboard


@pytest.mark.parametrize(
    ("requested", "count", "expected_page", "expected_total"),
    [(-3, 0, 0, 1), (0, 3, 0, 3), (2, 3, 2, 3), (99, 3, 2, 3)],
)
def test_bounded_page_handles_empty_first_last_and_out_of_range(
    requested, count, expected_page, expected_total
):
    assert auth.bounded_page(requested, count) == (expected_page, expected_total)


def test_client_navigation_keeps_main_menu_and_clamps_buttons():
    keyboard = request_navigation_keyboard(
        view="client",
        page=0,
        total_pages=2,
        menu=main_menu_keyboard("client"),
    )
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "requests:client:1" in callbacks
    assert "requests:client:-1" not in callbacks
    assert "menu:create" in callbacks
    assert "menu:profile" in callbacks


def test_staff_card_has_status_actions_and_menu():
    keyboard = request_navigation_keyboard(
        view="staff",
        page=0,
        total_pages=1,
        menu=main_menu_keyboard("master"),
        request_id=42,
        current_status="new",
    )
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "request:status:42:in_progress" in callbacks
    assert "request:status:42:completed" in callbacks
    assert "requests:staff:-1" not in callbacks
    assert "menu:profile" in callbacks


@pytest.mark.asyncio
async def test_client_request_callback_edits_screen_without_sending_message(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    user = SimpleNamespace(id=7, role="client")
    request = SimpleNamespace(
        id=13,
        status="new",
        priority="medium",
        description="Компьютер не включается после обновления.",
    )
    monkeypatch.setattr(auth, "SessionLocal", Session)
    monkeypatch.setattr(auth, "get_user_by_telegram_id", AsyncMock(return_value=user))
    monkeypatch.setattr(auth, "get_user_requests", AsyncMock(return_value=[request]))

    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    message = SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=100),
        message_id=200,
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )
    callback = SimpleNamespace(
        data="menu:requests",
        from_user=SimpleNamespace(id=55),
        message=message,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await auth.show_my_requests(callback, state)

    message.edit_text.assert_awaited_once()
    message.answer.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert "Заявка №13" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unchanged_screen_does_not_break_callback():
    bot = SimpleNamespace(edit_text=AsyncMock())
    bot.edit_text.side_effect = TelegramBadRequest(
        method=GetMe(),
        message="Bad Request: message is not modified",
    )
    callback = SimpleNamespace(
        from_user=None,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=100),
            message_id=200,
            edit_text=bot.edit_text,
        ),
    )

    await auth.edit_callback_screen(callback, "Тот же экран")

    bot.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_start_sends_a_new_screen(monkeypatch):
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    sent = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=201)
    message = SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=100),
        answer=AsyncMock(return_value=sent),
    )

    screen = await auth.show_start_screen(message, 55, "Приветствие")

    assert screen == (100, 201)
    bot.edit_message_text.assert_not_awaited()
    message.answer.assert_awaited_once_with("Приветствие", reply_markup=None, parse_mode=None)


@pytest.mark.asyncio
async def test_request_creation_sends_a_new_dialog_screen(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(auth, "SessionLocal", Session)
    monkeypatch.setattr(
        auth,
        "get_user_by_telegram_id",
        AsyncMock(return_value=SimpleNamespace(role="client")),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_id=200,
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=55),
        message=message,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock(), update_data=AsyncMock())

    await auth.start_request_creation(callback, state)

    callback.answer.assert_awaited_once()
    message.answer.assert_awaited_once()
    message.edit_text.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_description_sends_photo_prompt_without_editing_screen():
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=55),
        text="Компьютер не включается после выключения света.",
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())

    await auth.process_request_description(message, state)

    message.answer.assert_awaited_once()
    message.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_completion_sends_result_message(monkeypatch):
    request = SimpleNamespace(
        id=13,
        status="new",
        priority="medium",
        description="Компьютер не включается после выключения света.",
        ai_assessment=None,
    )
    monkeypatch.setattr(auth, "create_and_analyze_request", AsyncMock(return_value=(request, "client")))
    message = SimpleNamespace(from_user=SimpleNamespace(id=55), answer=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"description": request.description}),
        clear=AsyncMock(),
    )

    await auth.finish_request_creation(message, state)

    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_request_callback_keeps_access_check(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "")
    monkeypatch.setenv("MASTER_TELEGRAM_IDS", "")
    callback = SimpleNamespace(
        data="menu:all_requests",
        from_user=SimpleNamespace(id=55),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await auth.show_all_requests(callback, state)

    callback.answer.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_status_callback_updates_same_card_and_notifies_owner(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    owner = SimpleNamespace(full_name="Клиент", phone="+79990000000", telegram_id="77")
    old_request = SimpleNamespace(
        id=42,
        status="new",
        priority="medium",
        description="Перестал включаться компьютер.",
        user=owner,
    )
    request = SimpleNamespace(
        id=42,
        status="in_progress",
        priority="medium",
        description="Перестал включаться компьютер.",
        user=owner,
    )
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "55")
    monkeypatch.setattr(auth, "SessionLocal", Session)
    monkeypatch.setattr(auth, "get_repair_request", AsyncMock(side_effect=[old_request, request]))
    monkeypatch.setattr(auth, "update_request_status", AsyncMock(return_value=request))
    monkeypatch.setattr(auth, "get_all_requests", AsyncMock(return_value=[request]))
    monkeypatch.setattr(
        auth,
        "get_user_by_telegram_id",
        AsyncMock(return_value=SimpleNamespace(role="admin")),
    )

    bot = SimpleNamespace(send_message=AsyncMock())
    callback = SimpleNamespace(
        data="request:status:42:in_progress",
        from_user=SimpleNamespace(id=55),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=100),
            message_id=200,
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )

    await auth.change_request_status(callback, bot)

    callback.message.edit_text.assert_awaited_once()
    bot.send_message.assert_awaited_once_with("77", "Ваша заявка принята в работу.")
    assert "Заявка 1 из 1" in callback.message.edit_text.await_args.args[0]
