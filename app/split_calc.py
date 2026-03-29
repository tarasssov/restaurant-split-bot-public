from __future__ import annotations

from typing import Dict, List, Tuple


def calc_per_item_shares(items, participants: Dict[int, str]) -> Dict[int, int]:
    """
    Считает сумму по позициям на каждого по весам.
    Если у позиции не заданы веса — позиция игнорируется (MVP).
    """
    sums = {uid: 0 for uid in participants.keys()}

    for it in items:
        if not getattr(it, "weights", None):
            continue

        wsum = sum(max(v, 0) for v in it.weights.values())
        if wsum <= 0:
            continue

        for uid, w in it.weights.items():
            if uid not in sums:
                continue
            share = it.price * (max(w, 0) / wsum)
            sums[uid] += int(round(share))

    return sums


def apply_tip(subtotals: Dict[int, int], tip_percent: float, tip_fixed: int) -> Tuple[Dict[int, int], int, int]:
    """
    Возвращает:
      owed_total: сумма "должен всего" по людям (включая чаевые)
      tip_total: сумма чаевых
      subtotal_total: сумма по позициям
    """
    subtotal_total = sum(subtotals.values())
    if subtotal_total <= 0:
        return {k: 0 for k in subtotals}, 0, 0

    if tip_fixed and tip_fixed > 0:
        tip_total = int(tip_fixed)
    else:
        tip_total = int(round(subtotal_total * (tip_percent / 100.0)))

    owed_total: Dict[int, int] = {}
    distributed = 0
    uids = list(subtotals.keys())

    for i, uid in enumerate(uids):
        if i < len(uids) - 1:
            tip_part = int(round(tip_total * (subtotals[uid] / subtotal_total)))
            owed_total[uid] = subtotals[uid] + tip_part
            distributed += tip_part
        else:
            # остаток последнему, чтобы сумма чаевых сошлась
            tip_part = tip_total - distributed
            owed_total[uid] = subtotals[uid] + tip_part

    return owed_total, tip_total, subtotal_total


def balances(owed_total: Dict[int, int], paid: Dict[int, int]) -> Dict[int, int]:
    """
    balance = owed - paid
    >0 должен доплатить, <0 должен получить
    """
    out: Dict[int, int] = {}
    for uid, owe in owed_total.items():
        out[uid] = int(owe) - int(paid.get(uid, 0))
    return out


def min_transfers(balance: Dict[int, int]) -> List[Tuple[int, int, int]]:
    """
    Жадный алгоритм:
    должник с max доплатить -> получатель с max получить
    """
    debtors = [(uid, amt) for uid, amt in balance.items() if amt > 0]
    creditors = [(uid, -amt) for uid, amt in balance.items() if amt < 0]

    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)

    i = j = 0
    transfers: List[Tuple[int, int, int]] = []

    while i < len(debtors) and j < len(creditors):
        duid, damt = debtors[i]
        cuid, camt = creditors[j]

        x = min(damt, camt)
        if x > 0:
            transfers.append((duid, cuid, x))

        damt -= x
        camt -= x

        debtors[i] = (duid, damt)
        creditors[j] = (cuid, camt)

        if damt == 0:
            i += 1
        if camt == 0:
            j += 1

    return transfers
