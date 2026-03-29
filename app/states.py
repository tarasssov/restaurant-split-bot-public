from aiogram.fsm.state import StatesGroup, State

class ReceiptFlow(StatesGroup):
    waiting_photo = State()
    confirm_items = State()
    collect_participants = State()
    set_tip = State()
    set_weights = State()
    set_payments = State()
