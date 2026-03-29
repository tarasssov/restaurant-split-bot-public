# app/ocr_normalizer.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Tuple


# --- LAT <-> CYR confusables (похожие буквы) ---
_LAT2CYR: Dict[str, str] = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
}

_CYR2LAT: Dict[str, str] = {v: k for k, v in _LAT2CYR.items() if k.isupper()}
_CYR2LAT.update({v: k for k, v in _LAT2CYR.items() if k.islower()})

# часто OCR путает b/ь/ы и т.п. — не делаем опасные замены символов,
# только “похожие” лат/кир, чтобы не ломать русские слова.


# --- Fix dictionary for common OCR broken keywords ---
_WORD_FIXES: Dict[str, str] = {
    # Заголовок/служебное
    "FOCTEBOM": "ГОСТЕВОЙ",
    "CUET": "СЧЕТ",
    "OTKPBIT": "ОТКРЫТ",
    "OTKPBLT": "ОТКРЫТ",
    "OOVUNANT": "ОФИЦИАНТ",
    "3AN": "ЗАЛ",
    # Итоги
    "OTD": "ИТОГО",
    "OПЛATE": "ОПЛАТЕ",
    "OПЛATЕ": "ОПЛАТЕ",
    # Меню
    "KOMNAVMEHT": "КОМПЛИМЕНТ",
    "XRIE6": "ХЛЕБ",
    "HABOR": "НАБОР",
    "HACTOEB": "НАСТОЕВ",
}

def _keyify(token: str) -> str:
    """
    Превращает слово в "ключ", устойчивый к смеси латиницы/кириллицы confusables:
      - кириллические confusables -> латиница
      - латиница -> верхний регистр
      - оставляем цифры
    """
    out = []
    for ch in token:
        # сначала кириллица->латиница, если похоже
        if ch in _CYR2LAT:
            out.append(_CYR2LAT[ch])
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"[^A-Za-z0-9]+", "", s)
    return s.upper()


_WORD_FIXES_KEYED: Dict[str, str] = {_keyify(k): v for k, v in _WORD_FIXES.items()}


@dataclass(frozen=True)
class NormalizeReport:
    dropped_lines: int
    replaced_chars: int
    replaced_words: int


def _drop_obvious_garbage(lines: list[str]) -> Tuple[list[str], int]:
    """
    Убираем строки, которые почти целиком из латинских букв/мусора и не содержат ни цифр, ни кириллицы.
    Это обычно “шум OCR” типа: FIR AES AN ARN...
    """
    kept: list[str] = []
    dropped = 0

    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        has_digit = bool(re.search(r"\d", s))
        has_cyr = bool(re.search(r"[А-Яа-яЁё]", s))
        latin_letters = len(re.findall(r"[A-Za-z]", s))

        # много латиницы, нет цифр, нет кириллицы => мусор
        if (latin_letters >= 12) and (not has_digit) and (not has_cyr):
            dropped += 1
            continue

        kept.append(ln)

    return kept, dropped


def _lat_confusables_to_cyr(s: str) -> Tuple[str, int]:
    out = []
    changed = 0
    for ch in s:
        rep = _LAT2CYR.get(ch)
        if rep is None:
            out.append(ch)
        else:
            out.append(rep)
            changed += 1
    return "".join(out), changed


def _apply_word_fixes(text: str) -> Tuple[str, int]:
    replaced = 0

    def repl(m: re.Match) -> str:
        nonlocal replaced
        w = m.group(0)
        k = _keyify(w)
        if k in _WORD_FIXES_KEYED:
            replaced += 1
            return _WORD_FIXES_KEYED[k]
        return w

    # слова/токены: берем последовательности букв/цифр (включая смешанные)
    return re.sub(r"[A-Za-zА-Яа-яЁё0-9_]+", repl, text), replaced


def _normalize_money(text: str) -> str:
    t = text.replace("\u00A0", " ")

    # "12 350,00" -> "12350,00"
    # Важно: не склеиваем через \n, иначе ломаются колонки qty/price.
    t = re.sub(r"(?<=\d)[ \t]+(?=\d{3}(?:[.,]\d{2})?\b)", "", t)

    # "490,00" -> "490.00"
    t = re.sub(r"(\d),(\d{2})\b", r"\1.\2", t)

    return t


def normalize_ocr_text(raw_text: str) -> Tuple[str, NormalizeReport]:
    if raw_text is None:
        raw_text = ""

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    lines, dropped = _drop_obvious_garbage(lines)
    text = "\n".join(lines)

    # Сначала фиксим "словами" (работает даже на смешанных лат/кир),
    # потом переводим похожие латинские буквы в кириллицу для единообразия.
    text, words = _apply_word_fixes(text)
    text, chars = _lat_confusables_to_cyr(text)

    text = _normalize_money(text)

    # чистим пробелы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    return text, NormalizeReport(
        dropped_lines=dropped,
        replaced_chars=chars,
        replaced_words=words,
    )
