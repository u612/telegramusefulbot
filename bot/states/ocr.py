from aiogram.fsm.state import State, StatesGroup


class OCRStates(StatesGroup):
    waiting_for_language = State()
    waiting_for_image = State()
