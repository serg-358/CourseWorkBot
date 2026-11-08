"""Состояния создания и дополнения заявки."""

from aiogram.fsm.state import State, StatesGroup


class RequestStates(StatesGroup):
    waiting_description = State()
    waiting_photo = State()
    waiting_addition = State()
