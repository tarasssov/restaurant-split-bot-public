from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder


def kb_items_confirm():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить", callback_data="items_ok")
    b.button(text="🔄 Пересканировать (новое фото)", callback_data="items_rescan")
    b.adjust(1)
    return b.as_markup()


def kb_join():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Я участвую", callback_data="join")
    b.button(text="➕ Тестовый участник", callback_data="join_test")
    b.button(text="✅ Готово (к позициям)", callback_data="join_done")
    b.adjust(1)
    return b.as_markup()


def kb_tip():
    b = InlineKeyboardBuilder()
    b.button(text="Чаевые 0%", callback_data="tip_0")
    b.button(text="Чаевые 10%", callback_data="tip_10")
    b.button(text="Чаевые 12%", callback_data="tip_12")
    b.button(text="Чаевые 15%", callback_data="tip_15")
    b.button(text="Ввести % вручную", callback_data="tip_custom")
    b.button(text="Ввести ₽ вручную", callback_data="tip_fixed")
    b.adjust(2)
    return b.as_markup()


def kb_split_mode():
    b = InlineKeyboardBuilder()
    b.button(text="🧾 Разобрать по позициям", callback_data="split_mode_items")
    b.button(text="⚡ Разделить всё поровну", callback_data="split_mode_equal")
    b.adjust(1)
    return b.as_markup()


def kb_participants_toggle(participants: list[tuple[int, str]], selected: set[int], item_idx: int):
    """
    participants: [(user_id, name)]
    selected: set(user_id) - кто выбран по позиции
    """
    b = InlineKeyboardBuilder()
    for uid, name in participants:
        mark = "✅" if uid in selected else "☑️"
        b.button(text=f"{mark} {name}", callback_data=f"it:{item_idx}:tog:{uid}")
    b.button(text="Далее", callback_data=f"it:{item_idx}:done")
    b.adjust(1)
    return b.as_markup()


def kb_quick_weights(item_idx: int):
    b = InlineKeyboardBuilder()
    b.button(text="Поровну", callback_data=f"w:{item_idx}:equal")
    b.button(text="Ввести вручную", callback_data=f"w:{item_idx}:manual")
    b.adjust(2)
    return b.as_markup()
