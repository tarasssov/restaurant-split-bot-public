from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Item:
    name: str
    price: int  # RUB
    weights: Dict[int, float] = field(default_factory=dict)  # user_id -> weight


@dataclass
class Session:
    chat_id: int
    items: List[Item] = field(default_factory=list)
    total_rub: Optional[int] = None
    participants: Dict[int, str] = field(default_factory=dict)  # user_id -> display name
    tip_percent: float = 0.0
    tip_fixed: int = 0
    paid: Dict[int, int] = field(default_factory=dict)  # user_id -> paid RUB


class InMemoryStore:
    def __init__(self) -> None:
        self._sessions: Dict[int, Session] = {}

    def get(self, chat_id: int) -> Session:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = Session(chat_id=chat_id)
        return self._sessions[chat_id]

    def reset(self, chat_id: int) -> Session:
        self._sessions[chat_id] = Session(chat_id=chat_id)
        return self._sessions[chat_id]

    # --- items ---
    def set_items(self, chat_id: int, items: List[Item]) -> None:
        s = self.get(chat_id)
        s.items = items

    # --- participants ---
    def ensure_participant(self, chat_id: int, user_id: int, name: str) -> None:
        s = self.get(chat_id)
        if user_id not in s.participants:
            s.participants[user_id] = name

    def remove_participant(self, chat_id: int, user_id: int) -> None:
        s = self.get(chat_id)
        s.participants.pop(user_id, None)
        s.paid.pop(user_id, None)
        # также чистим веса по позициям
        for it in s.items:
            it.weights.pop(user_id, None)

    def set_paid(self, chat_id: int, user_id: int, amount: int) -> None:
        s = self.get(chat_id)
        s.paid[user_id] = max(0, int(amount))

    # --- tip ---
    def set_tip_percent(self, chat_id: int, value: float) -> None:
        s = self.get(chat_id)
        s.tip_percent = float(value)
        s.tip_fixed = 0

    def set_tip_fixed(self, chat_id: int, value: int) -> None:
        s = self.get(chat_id)
        s.tip_fixed = int(value)
        s.tip_percent = 0.0
