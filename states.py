"""Состояния диалога регистрации."""

from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    waiting_full_name = State()
    waiting_phone = State()


class RequestStates(StatesGroup):
    waiting_description = State()
    waiting_photo = State()
