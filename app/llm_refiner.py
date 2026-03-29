from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.storage import Item

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import httpx
except Exception:
    httpx = None


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, "", "None"):
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


@dataclass
class LLMResult:
    items: List[Item]
    notes: Optional[str] = None

    # чтобы работало: items, notes = res
    def __iter__(self):
        yield self.items
        yield self.notes


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "", "None") else default


def _make_openai_client(api_key: str):
    proxy_url = (os.getenv("OPENAI_PROXY_URL") or os.getenv("OPENAI_ALL_PROXY") or "").strip()
    if proxy_url and httpx is not None:
        return OpenAI(api_key=api_key, http_client=httpx.Client(proxy=proxy_url, timeout=60.0))
    return OpenAI(api_key=api_key)


def _norm_key(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ё", "е")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_volume_ml(name: str) -> Optional[str]:
    """
    Ищем "40 мл", "0.5 л", "0,3 л" и т.п.
    Возвращаем нормализованно: "40мл" / "0.5л"
    """
    n = name.lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(мл|ml|л|l)\b", n)
    if not m:
        return None
    val = m.group(1)
    unit = m.group(2)
    unit = "мл" if unit in ("мл", "ml") else "л"
    return f"{val}{unit}"


def merge_same_alcohol(items: List[Item]) -> List[Item]:
    """
    Объединяем ТОЛЬКО алкоголь:
    - по совпадению (нормализованного) названия + объёма
    - суммируем цену
    """
    alcohol_words = ("водка", "пиво", "вино", "настойка", "джин", "ром", "виски", "ликер", "шампан", "сидр", "коньяк", "бренди", "текила")
    buckets = {}
    others: List[Item] = []

    for it in items:
        key_name = _norm_key(it.name)
        is_alc = any(w in key_name for w in alcohol_words)
        vol = _extract_volume_ml(it.name) if is_alc else None

        if is_alc and vol:
            k = (key_name, vol)
            if k not in buckets:
                buckets[k] = Item(name=it.name, price=it.price)
            else:
                buckets[k].price += it.price
        else:
            others.append(it)

    # возвращаем: сначала "не алкоголь", потом объединённый алкоголь (стабильно)
    merged_alc = list(buckets.values())
    return others + merged_alc


def _add_adjustment_if_needed(items: List[Item], total_rub: Optional[int]) -> Tuple[List[Item], Optional[int]]:
    if total_rub is None:
        return items, None
    s = sum(i.price for i in items)
    diff = total_rub - s
    if diff == 0:
        return items, 0
    # добавляем позицию корректировки
    adj = Item(name="Корректировка по итогу", price=diff)
    return items + [adj], diff


def llm_refine_receipt_items(
    ocr_text: str,
    parsed_items: List[Item],
    total_rub: Optional[int],
    model: Optional[str] = None,
) -> LLMResult:
    """
    ВАЖНО: возвращаем LLMResult, который можно распаковать:
        items, notes = llm_refine_receipt_items(...)
    """
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")

    if OpenAI is None:
        raise RuntimeError("openai package is not installed")

    model = model or _env("OPENAI_MODEL", "gpt-4.1-mini")
    excerpt_chars = max(2000, _env_int("LLM_OCR_EXCERPT_CHARS", 9000))

    # минимальная структура входа для модели
    payload = {
        "total_rub": total_rub,
        "parsed_items": [{"name": it.name, "price": it.price} for it in parsed_items],
        "ocr_text_excerpt": (ocr_text or "")[:excerpt_chars],
        "rules": [
            "Игнорируй разбиение по гостям. Считай, что чек общий.",
            "Не придумывай товары, которых нет в OCR-тексте.",
            "Можно объединять одинаковые алкогольные позиции только если совпадает название и объем.",
            "Если суммы не хватает до total_rub, добавь 'Корректировка по итогу' одной строкой.",
            "Для каждой позиции добавь OCR-evidence: line_ids, evidence_text, confidence.",
        ],
        "output_schema": {
            "items": [
                {
                    "name": "str",
                    "price": "int",
                    "evidence_line_ids": ["int"],
                    "evidence_text": "str|null",
                    "confidence": "float|null",
                }
            ],
            "notes": "str",
        }
    }

    client = _make_openai_client(api_key)

    # максимально совместимо: без response_format (чтобы не ловить ошибки SDK)
    resp = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": "Ты аккуратный нормализатор ресторанных чеков. Верни только валидный JSON строго по схеме."
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False)
            }
        ],
    )

    text = (resp.output_text or "").strip()
    # пытаемся вытащить JSON даже если модель завернула в текст
    m = re.search(r"\{.*\}\s*$", text, flags=re.S)
    if m:
        text = m.group(0)

    try:
        data = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"LLM returned non-JSON: {e}. Raw: {text[:400]}")

    out_items: List[Item] = []
    for row in data.get("items", []):
        name = str(row.get("name", "")).strip()
        price = row.get("price", row.get("price_rub", None))
        try:
            price = int(price)
        except Exception:
            continue
        if not name or price == 0:
            continue
        out_items.append(Item(name=name, price=price))

    notes = data.get("notes")
    if notes is not None:
        notes = str(notes).strip() or None

    # объединяем алкоголь (строго по названию+объему)
    out_items = merge_same_alcohol(out_items)

    # доводим до total, если он есть
    out_items, adj = _add_adjustment_if_needed(out_items, total_rub)
    if adj and adj != 0:
        extra = f" | Корректировка по итогу: {adj:+d} ₽"
        notes = (notes or "") + extra if notes else extra.strip(" |")

    return LLMResult(items=out_items, notes=notes)
