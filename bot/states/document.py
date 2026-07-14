from aiogram.fsm.state import State, StatesGroup


class DocumentStates(StatesGroup):
    waiting_for_file_convert = State()
  
