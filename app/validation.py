from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional, Tuple

from app.storage import Item


@dataclass
class ValidationReport:
    ok: bool
    issues: List[str]
    items_sum: int
    total_rub: Optional[int]
    diff: Optional[int]
    suspicious_items: List[Tuple[int, str, int]]  # (idx, name, price)


_BAD_NAME_RE = re.compile(
    r"\b(заказ|гостев|сч[её]т|касса|инн|кпп|официант|стол|открыт|наименование|сумма|кол-во|итого|к оплате)\b",
    re.IGNORECASE
)

_HAS_LETTERS_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def validate_items(
    items: List[Item],
    total_rub: Optional[int],
    *,
    min_items: int = 3,
) -> ValidationReport:
    issues: List[str] = []
    suspicious: List[Tuple[int, str, int]] = []

    items_sum = sum(int(it.price) for it in items)

    # 1) Минимальное число позиций
    if len(items) < min_items:
        issues.append(f"too_few_items:{len(items)}")

    # 2) Плохие/служебные названия и странные цены
    for idx, it in enumerate(items, start=1):
        name = (it.name or "").strip()
        price = int(it.price)

        if not name or not _HAS_LETTERS_RE.search(name) or len(name) < 3:
            suspicious.append((idx, name, price))
            continue

        if _BAD_NAME_RE.search(name):
            suspicious.append((idx, name, price))
            continue

        if price <= 0 or price > 500_000:
            suspicious.append((idx, name, price))
            continue

        # частая ошибка: “Заказ № 2334 — 2334 ₽”
        if re.search(r"\b\d{3,6}\b", name) and re.search(r"\bзаказ\b", name, re.I):
            suspicious.append((idx, name, price))

    if suspicious:
        issues.append(f"suspicious_items:{len(suspicious)}")

    # 3) Сверка суммы с ИТОГО
    diff: Optional[int] = None
    if total_rub is not None:
        diff = items_sum - int(total_rub)
        # допуск: минимум 50₽ или 1% от итога (что больше)
        tolerance = max(50, int(round(total_rub * 0.01)))
        if abs(diff) > tolerance:
            issues.append(f"sum_mismatch:diff={diff}:tol={tolerance}")

    ok = len(issues) == 0

    return ValidationReport(
        ok=ok,
        issues=issues,
        items_sum=items_sum,
        total_rub=total_rub,
        diff=diff,
        suspicious_items=suspicious,
    )


def format_validation_report(rep: ValidationReport) -> str:
    lines: List[str] = []
    lines.append("🧾 Проверка распознавания:")

    if rep.total_rub is not None:
        sign = "+" if (rep.diff or 0) > 0 else ""
        lines.append(f"• Сумма позиций: {rep.items_sum} ₽")
        lines.append(f"• Итого в чеке: {rep.total_rub} ₽")
        lines.append(f"• Разница: {sign}{rep.diff} ₽")
    else:
        lines.append(f"• Сумма позиций: {rep.items_sum} ₽")
        lines.append("• Итого в чеке: (не найдено в OCR)")

    if rep.ok:
        lines.append("✅ Выглядит нормально, можно продолжать.")
        return "\n".join(lines)

    # explain issues in human words
    lines.append("⚠️ Есть проблемы:")
    for code in rep.issues:
        if code.startswith("too_few_items"):
            n = code.split(":")[1]
            lines.append(f"• Мало позиций ({n}). Часто это признак плохого OCR/парсинга.")
        elif code.startswith("suspicious_items"):
            n = code.split(":")[1]
            lines.append(f"• Есть подозрительные позиции ({n}) — похожи на служебные строки или мусор.")
        elif code.startswith("sum_mismatch"):
            # sum_mismatch:diff=...:tol=...
            m = re.search(r"diff=([-\d]+):tol=(\d+)", code)
            if m:
                lines.append(f"• Сумма позиций не сходится с итогом (разница {m.group(1)} ₽, допуск {m.group(2)} ₽).")
            else:
                lines.append("• Сумма позиций не сходится с итогом.")
        else:
            lines.append(f"• {code}")

    if rep.suspicious_items:
        lines.append("")
        lines.append("Подозрительные строки (проверь глазами):")
        for idx, name, price in rep.suspicious_items[:8]:
            nm = name if name else "(пусто)"
            lines.append(f"• {idx}) {nm} — {price} ₽")
        if len(rep.suspicious_items) > 8:
            lines.append(f"… и ещё {len(rep.suspicious_items)-8}")

    lines.append("")
    lines.append("Совет: переснять чек (ровнее/ближе/без бликов) или продолжить и поправить вручную.")
    return "\n".join(lines)
