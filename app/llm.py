from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from openai import OpenAI
import httpx

from app.storage import Item


# -------------------------
# Config
# -------------------------
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


@dataclass
class LLMParseResult:
    items: List[Item]
    total_rub: Optional[int] = None
    notes: Optional[str] = None


def _coerce_int(x) -> Optional[int]:
    try:
        v = int(round(float(x)))
        if v <= 0:
            return None
        if v > 500_000:
            return None
        return v
    except Exception:
        return None


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, "", "None"):
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


def _make_openai_client() -> OpenAI:
    proxy_url = (os.getenv("OPENAI_PROXY_URL") or os.getenv("OPENAI_ALL_PROXY") or "").strip()
    if proxy_url:
        return OpenAI(http_client=httpx.Client(proxy=proxy_url, timeout=60.0))
    return OpenAI()


def _safe_json_loads(s: str) -> dict:
    # иногда модель может вернуть пробелы/переносы — ок
    return json.loads(s.strip())


_ALC_WORDS = (
    "водка", "пиво", "вино", "настойка", "виски", "ром", "джин", "ликер",
    "коньяк", "бренди", "текила", "шампан", "сидр", "шот",
)
_MONEY_TOKEN = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00A0]\d{3})*|\d+)(?:[\,\.](\d{1,2}))?(?!\d)")


def _is_alcohol_name(name: str) -> bool:
    low = (name or "").lower()
    return any(w in low for w in _ALC_WORDS)


def _has_volume(name: str) -> bool:
    low = (name or "").lower()
    return bool(re.search(r"\b\d+(?:[\,\.]\d+)?\s*(мл|ml|л|l)\b", low))


def _money_values_rub(line: str) -> List[int]:
    vals: List[int] = []
    for m in _MONEY_TOKEN.finditer(line or ""):
        i = m.group(1).replace(" ", "").replace("\u00A0", "")
        d = m.group(2)
        if d is None:
            v = int(i)
        else:
            d2 = (d + "0")[:2]
            v = int(round(float(f"{i}.{d2}")))
        # отсекаем qty/объемы/шум
        if 80 <= v <= 6000:
            vals.append(v)
    return vals


def _reprice_large_alcohol_items(items: List[Item], ocr_text: str, total_rub: Optional[int]) -> List[Item]:
    """
    Если LLM сделал одну слишком большую алкогольную строку, пытаемся
    переоценить её из OCR-линий с ценами для этого же напитка.
    """
    if not items or not total_rub:
        return items

    lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
    out: List[Item] = []

    for it in items:
        price = int(it.price)
        name = it.name or ""
        share = price / max(1, abs(int(total_rub)))

        if not (_is_alcohol_name(name) and _has_volume(name) and share > 0.35):
            out.append(it)
            continue

        low_name = name.lower()
        key_tokens = [t for t in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", low_name) if t not in _ALC_WORDS]
        key_tokens = key_tokens[:3]

        cands: List[int] = []
        for ln in lines:
            low_ln = ln.lower()
            if not any(w in low_ln for w in _ALC_WORDS):
                continue
            if key_tokens and not any(t in low_ln for t in key_tokens):
                continue
            if not _has_volume(low_ln):
                continue
            cands.extend(_money_values_rub(ln))

        # Убираем полные дубли подряд и слишком большие выбросы.
        filtered: List[int] = []
        for v in cands:
            if not filtered or filtered[-1] != v:
                filtered.append(v)
        cands = [v for v in filtered if v <= price]

        if len(cands) >= 2:
            alt_price = sum(cands)
            # принимаем только если действительно "разлепили" крупный merge
            if 0 < alt_price < price and alt_price >= int(price * 0.45):
                out.append(Item(name=it.name, price=alt_price))
                continue

        out.append(it)

    return out


def llm_parse_receipt(
    ocr_text: str,
    hint_total_rub: Optional[int] = None,
    hint_items_sum: Optional[int] = None,
    max_items: int = 80,
    model: Optional[str] = None,
) -> LLMParseResult:
    """
    LLM-парсер: получает OCR-текст и возвращает список Item(name, price).
    Особенности:
    - игнорирует сервисные строки
    - игнорирует комплименты/нулевые цены
    - объединяет ТОЛЬКО алкоголь с одинаковым названием (водка/вино/пиво/виски/шоты и т.п.)
    - если есть ИТОГО и одна цена потерялась (как говядина vs водка) — старается восстановить по разнице
    """

    if not ocr_text or len(ocr_text.strip()) < 30:
        return LLMParseResult(items=[], total_rub=hint_total_rub, notes="OCR text too short")

    client = _make_openai_client()
    model_name = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    excerpt_chars = max(2000, _env_int("LLM_OCR_EXCERPT_CHARS", 9000))
    ocr_excerpt = (ocr_text or "")[:excerpt_chars]

    schema = {
        "name": "receipt_items",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "total_rub": {"type": ["integer", "null"]},
                "notes": {"type": ["string", "null"]},
                "items": {
                    "type": "array",
                    "maxItems": max_items,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "price_rub": {"type": "integer"},
                            "is_alcohol": {"type": "boolean"},
                            "qty_note": {"type": ["string", "null"]},  # например "12.5 шота"
                            "evidence_line_ids": {
                                "type": ["array", "null"],
                                "items": {"type": "integer"},
                            },
                            "evidence_text": {"type": ["string", "null"]},
                            "confidence": {"type": ["number", "null"]},
                        },
                        "required": ["name", "price_rub", "is_alcohol"],
                    },
                },
            },
            "required": ["items"],
        },
        "strict": True,
        "type": "json_schema",
    }

    sys = (
        "Ты — аккуратный экстрактор позиций из OCR-текста ресторанного чека на русском.\n"
        "Нужно вернуть JSON строго по схеме.\n"
        "\n"
        "Правила:\n"
        "1) Верни только реальные позиции меню/напитков с ценой (рубли). Сервисные строки (ИНН, касса, зал, стол, официант, заказ, итого, спасибо) игнорируй.\n"
        "2) Комплименты/нулевые позиции (0,00) НЕ возвращай.\n"
        "3) Цены: целые рубли. Пример: '2 250,00' => 2250.\n"
        "4) Алкоголь: если в чеке встречается один и тот же алкоголь (водка/вино/пиво/виски/шоты и т.п.) несколько раз, ОБЪЕДИНИ его в одну строку (один item) с суммарной ценой.\n"
        "   - В name можно добавить qty_note в скобках, например: 'Водка Царская 40 мл (12.5 шота)'.\n"
        "   - Объединять ТОЛЬКО алкоголь. Еду/десерты НЕ объединяй даже если названия одинаковые.\n"
        "5) Если видишь итоговую сумму по чеку и понимаешь, что одна позиция потерялась/слиплась (например еда без цены рядом с ценой алкоголя),\n"
        "   попробуй восстановить цену этой еды по разнице (итого - сумма других позиций), только если это выглядит однозначно.\n"
        "6) total_rub: если в тексте есть 'ИТОГО'/'К ОПЛАТЕ' — заполни total_rub. Иначе null.\n"
        "7) Для каждой позиции добавь OCR evidence: evidence_line_ids, evidence_text и confidence [0..1].\n"
    )

    user = (
        "Вот OCR-текст чека. Вытащи позиции.\n"
        f"Подсказка: hint_total_rub={hint_total_rub}, hint_items_sum={hint_items_sum}\n"
        "\n"
        "OCR:\n"
        "-----\n"
        f"{ocr_excerpt}\n"
        "-----\n"
    )

    # Совместимость со старыми версиями SDK:
    # если response_format не поддерживается, делаем fallback на plain text JSON.
    try:
        resp = client.responses.create(
            model=model_name,
            input=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
            response_format=schema,  # Structured Outputs (json_schema)
        )
    except TypeError:
        resp = client.responses.create(
            model=model_name,
            input=[
                {
                    "role": "system",
                    "content": sys + "\nВерни только валидный JSON, без комментариев и markdown.",
                },
                {"role": "user", "content": user},
            ],
        )

    out_text = (resp.output_text or "").strip()
    if not out_text:
        return LLMParseResult(items=[], total_rub=hint_total_rub, notes="empty model output")

    # fallback: вырезаем JSON-объект из свободного текста
    if not out_text.startswith("{"):
        import re
        m = re.search(r"\{.*\}\s*$", out_text, flags=re.S)
        if m:
            out_text = m.group(0)

    data = _safe_json_loads(out_text)

    total_rub = data.get("total_rub", None)
    total_rub = _coerce_int(total_rub) if total_rub is not None else None

    def _pick_name(row: dict) -> str:
        for k in ("name", "title", "item", "position"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _pick_price(row: dict) -> Optional[int]:
        for k in ("price_rub", "price", "amount", "rub", "sum", "total"):
            if k in row:
                v = _coerce_int(row.get(k))
                if v:
                    return v
        return None

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    items: List[Item] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue

        name = _pick_name(it)
        price = _pick_price(it)
        if not name or not price:
            continue

        qty_note = it.get("qty_note")
        if qty_note:
            qty_note = str(qty_note).strip()
            if qty_note and "(" not in name:
                name = f"{name} ({qty_note})"

        items.append(Item(name=name, price=price))

    # Fallback: иногда модель может вернуть объект {"lines":[...]} вместо items.
    if not items:
        lines = data.get("lines", [])
        if isinstance(lines, list):
            for row in lines:
                if not isinstance(row, dict):
                    continue
                name = _pick_name(row)
                price = _pick_price(row)
                if name and price:
                    items.append(Item(name=name, price=price))

    items = _reprice_large_alcohol_items(items, ocr_text, total_rub or hint_total_rub)

    notes = data.get("notes")
    if notes is not None:
        notes = str(notes).strip() or None

    return LLMParseResult(items=items, total_rub=total_rub, notes=notes)


def llm_reconcile_receipt(
    ocr_text: str,
    current_items: List[Item],
    total_rub: Optional[int] = None,
    max_items: int = 80,
    model: Optional[str] = None,
) -> LLMParseResult:
    """
    Третий этап LLM: reconcile на базе уже найденного списка.
    Важно: это всё ещё "limited budget" шаг, запускается только при низком качестве.
    """
    if not ocr_text or len(ocr_text.strip()) < 30:
        return LLMParseResult(items=[], total_rub=total_rub, notes="OCR text too short")

    preview = "\n".join(
        f"- {it.name} | {int(it.price)}"
        for it in (current_items or [])[:max_items]
    )
    augmented = (
        (ocr_text or "")
        + "\n\n=== CURRENT_ITEMS_BASELINE ===\n"
        + preview
        + "\n=== END_CURRENT_ITEMS_BASELINE ===\n"
    )
    return llm_parse_receipt(
        augmented,
        hint_total_rub=total_rub,
        hint_items_sum=sum(int(i.price) for i in (current_items or [])),
        max_items=max_items,
        model=model,
    )
