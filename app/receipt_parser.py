from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional, Tuple

from app.ocr_normalizer import normalize_ocr_text


@dataclass(frozen=True)
class ReceiptItem:
    name: str
    price: int  # RUB
    meta: dict | None = None


# ----------------- Normalization -----------------

_WS_RE = re.compile(r"\s+")
_FANCY_MAP = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "—": "-",
        "–": "-",
        "…": "...",
        "\u00A0": " ",
    }
)


def _clean_line(s: str) -> str:
    s = (s or "").translate(_FANCY_MAP)
    # OCR часто склеивает "qty price" в один токен: 1.00239.00 -> 1.00 239.00.
    # Расклеиваем только правдоподобные qty-цены, чтобы не ломать 0,5250.00 и 5,250.
    def _split_stuck_qty_price(m: re.Match[str]) -> str:
        q_raw = m.group(1)
        p_raw = m.group(2)
        try:
            q = float(q_raw.replace(",", "."))
            p = float(p_raw.replace(",", "."))
        except Exception:
            return m.group(0)
        if 0.8 <= q <= 20.0 and p >= 80.0:
            return f"{q_raw} {p_raw}"
        return m.group(0)

    s = re.sub(
        r"(?<!\d)(\d{1,2}[\,\.]\d{2})(\d{2,6}[\,\.]\d{2})(?!\d)",
        _split_stuck_qty_price,
        s,
    )
    s = s.replace("\r", "")
    s = _WS_RE.sub(" ", s).strip()
    return s


def _compact(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (s or "").lower())


# ----------------- Money token parsing -----------------

# 12 350,00 | 12350.00 | 1500.0 | 2,80 | 1,00 | 900
_MONEY_TOKEN = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00A0]\d{3})*|\d+)(?:[\,\.](\d{1,2}))?(?!\d)"
)


def _parse_money_tokens(line: str) -> List[Tuple[float, bool, str | None]]:
    """Return list of (value_as_float, had_decimals_flag, raw_decimals)."""
    out: List[Tuple[float, bool, str | None]] = []
    for m in _MONEY_TOKEN.finditer(line):
        int_part = m.group(1).replace(" ", "").replace("\u00A0", "")
        dec = m.group(2)
        has_dec = dec is not None
        if dec is None:
            val = float(int(int_part))
        else:
            dec2 = (dec + "0")[:2]
            val = float(f"{int_part}.{dec2}")
        out.append((val, has_dec, dec))
    return out


def _to_amount(val: float, has_decimals: bool, mode: str) -> int:
    if mode == "cents":
        if has_decimals:
            return int(round(val * 100))
        return int(round(val))
    return int(round(val))


# ----------------- Receipt type detection -----------------

_GUEST_HEADER_RE = re.compile(r"\bгость\s*(\d+)\b", re.IGNORECASE)
_GUEST_SUBTOTAL_RE = re.compile(r"итого\s+к\s+оплате\s+гость\s*\d+", re.IGNORECASE)


def _is_guest_receipt(text: str) -> bool:
    low = text.lower()
    if "гостевой счет" in low or "гостевой сч" in low:
        return True
    if len(re.findall(r"итого\s+к\s+оплате\s+гость", low)) >= 2:
        return True
    if len(re.findall(r"\bгость\s*\d+\b", low)) >= 2 and "итого к оплате" in low:
        return True
    return False


def _is_decimal_table_receipt(text: str) -> bool:
    """
    Табличные чеки типа "Цена / Кол-во / Сумма".
    Используем только как подсказку выбора суммы в строке.
    """
    low = text.lower()
    if "блюдо" in low and ("кол-во" in low or "колво" in low) and "сумма" in low:
        return True
    if "цена" in low and ("кол-во" in low or "колво" in low) and "сумма" in low:
        return True
    if len(re.findall(r"\d+[\,\.]\d{1,2}\s+\d+[\,\.]\d{1,2}\s+\d+[\,\.]\d{1,2}", low)) >= 3:
        return True
    return False


def _receipt_kind(text: str) -> str:
    if _is_guest_receipt(text):
        return "guest"
    if _is_decimal_table_receipt(text):
        return "table"
    return "line"


# ----------------- Total extraction -----------------

_TOTAL_MARKERS = [
    "итого к оплате",
    "сумма к оплате",
    "полная сумма",
    "итого:",
    "итого",
    "к оплате",
    "всего",
    "total",
    "итог:",
    "итог",
]
_TOTAL_MARKERS_COMPACT = [
    "итогокоплате",
    "суммакоплате",
    "полнаясумма",
    "итого",
    "коплате",
    "всего",
    "total",
    "итог",
]


def _has_total_marker(line: str) -> bool:
    low = line.lower()
    if any(m in low for m in _TOTAL_MARKERS):
        return True
    c = _compact(line)
    return any(m in c for m in _TOTAL_MARKERS_COMPACT)


def _money_mode(text: str) -> str:
    # Для чеков вида 123,90/2,80 храним суммы в cents.
    lines = [_clean_line(l) for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if l]

    for i in range(len(lines) - 1, -1, -1):
        if not _has_total_marker(lines[i]):
            continue
        window = [lines[i]] + lines[i + 1 : i + 4]
        for w in window:
            toks = _parse_money_tokens(w)
            if not toks:
                continue
            _, has_dec, dec = max(toks, key=lambda t: t[0])
            if has_dec and dec not in (None, "", "0", "00"):
                return "cents"
            return "rub"

    return "rub"


def extract_total_rub(text: str) -> Optional[int]:
    if not isinstance(text, str):
        return None

    try:
        text, _ = normalize_ocr_text(text)
    except Exception:
        pass

    mode = _money_mode(text)
    lines = [_clean_line(l) for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if l]

    for i in range(len(lines) - 1, -1, -1):
        if _has_total_marker(lines[i]):
            window = [lines[i]] + lines[i + 1 : i + 4]
            for w in window:
                tokens = _parse_money_tokens(w)
                if not tokens:
                    continue
                # для total обычно подходит самое большое число в окне
                val, has_dec, _ = max(tokens, key=lambda t: t[0])
                amount = _to_amount(val, has_decimals=has_dec, mode=mode)
                if 1 <= amount <= 500_000:
                    # В окне рядом с "ИТОГО" часто попадается qty=1;
                    # не завершаем поиск, а продолжаем искать реальную сумму.
                    if amount < 100:
                        continue
                    return amount

    # Fallback: берём последний разумный денежный токен с конца
    for l in lines[::-1]:
        toks = _parse_money_tokens(l)
        if not toks:
            continue
        vals = sorted((_to_amount(v, has_decimals=h, mode=mode) for v, h, _ in toks), reverse=True)
        for amount in vals:
            if 1 <= amount <= 500_000:
                if amount < 100:
                    continue
                return amount

    return None


# ----------------- Parsing items -----------------

_SERVICE_HINTS = [
    "ндс",
    "не облагается",
    "безналич",
    "кассир",
    "официант",
    "гостей",
    "гость",
    "стол",
    "заказ",
    "чек",
    "дата",
    "открыт",
    "закрыт",
    "печать",
    "наименование",
    "блюдо",
    "кол-во",
    "колво",
    "сумма",
    "цена",
    "итого",
    "всего",
    "спасибо",
    "ждем",
    "qr",
    "scan",
    "tip",
    "чаевые",
    "оплати",
    "guest name",
    "подпись",
    "sign",
    "room",
    "комн",
    "mysbertips",
    "pay.",
    "ооо",
    "ресторан",
    "restaurant",
    "hotel",
    "добр",
    "welcome",
    "южно-сахалинск",
    "область",
    "город",
    "пр-кт",
    "проспект",
    "д.",
    "дом",
    "приход",
    "ул.",
    "улица",
    "инн",
    "кпп",
    "скидка",
    "с учетом скидки",
    "итого к оплате",
    "счет",
    "рубли",
    "комплимент",
    "скидка",
    "подытог",
    "полная сумма",
    "сумма по",
    "итого по предприятиям",
    "гостевой счет",
    "qr код активен",
    "итого бар",
    "итого кухня",
    "итого мангал",
    "к оплате бар",
    "к оплате кухня",
    "полная сумма",
    "подытог",
    "сумма по ооо",
    "сумма по ип",
    "сумма заказов",
    "сотрудник",
    "товар",
    "кол-в",
    "вписать сумму",
    "всего по",
    "скидок/надбавок",
    "надбавок",
    "алкоголь",
    "еда",
    "всегда вам рады",
    "касса",
    "безналичными",
    "место расчетов",
    "количество предметов расчета",
    "сумма без ндс",
    "кассовый чек",
    "зн ккт",
    "рн ккт",
    "фд",
    "фп",
    "фн",
    "теремок",
]


def _looks_like_service(line: str) -> bool:
    low = line.lower()
    if any(h in low for h in _SERVICE_HINTS):
        return True

    compact = _compact(line)
    if any(x in compact for x in ("итого", "всего", "коплате", "guestname", "scan")):
        return True

    # дата/время/номерные заголовки
    if re.search(r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b", line):
        return True
    if re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", line):
        return True

    # Header lines like "3001 Имя Фамилия" are usually guest/card metadata.
    if re.match(r"^\s*\d{3,6}\s+[A-Za-zА-Яа-яЁё]{2,}\s+[A-Za-zА-Яа-яЁё]{2,}\s*$", line):
        return True
    # Full personal name in receipt footer/header.
    if re.match(r"^\s*[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\s*$", line):
        return True
    # Address-like receipt footer lines.
    if re.search(r"\b(область|город|г\.\s|пр-кт|проспект|ул\.|улица|дом|д\.)\b", low):
        return True
    if re.search(r"\bстоп:\s*\d+\b", low):
        return True

    # OCR-футер: одиночные ФИО-фрагменты (например, "Романович")
    if re.match(r"^\s*[A-ZА-ЯЁ][a-zа-яё]{5,}\s*$", line):
        lw = line.strip().lower()
        if lw.endswith(("ович", "евич", "ична", "вна", "овна", "евна")):
            return True

    # Налоговые служебные строки
    if re.search(r"\bндс\s+не\s+облагается\b", low):
        return True

    # OCR-футер: полное ФИО (2-4 слова), если есть отчество
    if re.match(r"^\s*[A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,3}\s*$", line):
        parts = [w.strip().lower() for w in line.split()]
        if any(w.endswith(("ович", "евич", "ична", "вна", "овна", "евна")) for w in parts):
            return True

    # OCR-обрезок адреса (например: "ина, д")
    if re.match(r"^\s*[а-яё]{2,8},\s*[а-яё]\.??\s*$", low):
        return True

    return False


def _is_pure_money_line(line: str) -> bool:
    s = line.replace(" ", "").replace("\u00A0", "")
    return bool(re.fullmatch(r"\d+(?:[\,\.]\d{1,2})?", s))


def _has_volume_hint(line: str) -> bool:
    # tolerant to OCR mixed scripts: ml/mL/mл/мl and л/l
    low = (line or "").lower()
    return bool(re.search(r"([mм][lл]|[lл])", low))


def _has_explicit_volume_unit(line: str) -> bool:
    low = (line or "").lower()
    if re.search(r"\b\d{1,4}\s*(мл|ml|л)\b", low):
        return True
    if re.search(r"\b(мл|ml)\b", low):
        return True
    return False


def _has_letters(s: str) -> bool:
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", s))


def _is_plausible_item_name(name: str) -> bool:
    n = _clean_line(name)
    if not n:
        return False

    compact = _compact(n)
    if not compact:
        return False

    if compact.isdigit():
        return False

    if compact in {"si", "s1", "i", "l"}:
        return False

    if len(re.findall(r"[A-Za-zА-Яа-яЁё]", n)) < 2:
        return False

    low = n.lower()
    if n.startswith("~"):
        return False
    if "_" in n:
        return False
    if low in {"мл", "ml", "л", "шт", "г", "кг", "рубли"}:
        return False
    if low in {"бар", "кухня", "мангал"}:
        return False
    if re.fullmatch(r"\d{1,4}\s*(мл|ml|л|шт|г|кг)\.?", low):
        return False

    if _looks_like_service(n):
        return False

    # Пример мусора: "...", "_", "SI:"
    if re.fullmatch(r"[\W_]+", n):
        return False

    banned_compact_parts = (
        "guestname", "mysbertips", "scan", "подпись", "комн", "room", "чек",
        "стл", "гст", "заказ", "кассир", "официант", "оплате", "итого",
        "всего", "кодсотрудника", "комплимент", "трансля", "риканскаякух",
    )
    if any(p in compact for p in banned_compact_parts):
        return False

    return True


def _is_condiment_like(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in ("соус", "аджик", "хлеб"))


def _join_name(parts: List[str]) -> str:
    name = " ".join(p for p in parts if p).strip()
    name = _WS_RE.sub(" ", name)
    return name[:180] if len(name) > 180 else name


_NOISE_CUT_MARKERS = (
    "псб",
    "блюдо",
    "debit",
    "dевiт",
    "о rub",
    "о ruв",
    "руб",
    "зал:",
    "зал ",
    "чек",
    "стол",
    "гостей",
    "кассир",
    "официант",
    "россия",
    "чаб",
    "всего по",
    "итого по",
    "скидка",
    "надбавок",
    "алкоголь",
    "еда",
)

_SPLIT_ANCHORS = (
    "салат", "уши", "борщ", "водка", "чай", "лимонад", "пиво",
    "настойка", "оладушки", "драники", "говядина", "грузди",
)
_BEVERAGE_ANCHORS = ("водка", "пиво", "чай", "лимонад", "настойка")


def _cleanup_item_name(name: str) -> str:
    n = _clean_line(name)
    if not n:
        return n

    low = n.lower()
    cut_pos = -1
    for m in _NOISE_CUT_MARKERS:
        pos = low.rfind(m)
        if pos > cut_pos:
            cut_pos = pos + len(m)
    if cut_pos > 0 and cut_pos < len(n):
        n = _clean_line(n[cut_pos:])

    toks = n.split()
    if len(toks) >= 2 and len(toks[0]) <= 2 and re.fullmatch(r"[A-Za-zА-Яа-яЁё]+", toks[0] or ""):
        toks = toks[1:]
    if len(toks) >= 3:
        tail = (toks[-1] or "").lower()
        if re.fullmatch(r"[а-яё]{1,3}", tail) and tail not in {"мл", "л"}:
            toks = toks[:-1]

    n = _clean_line(" ".join(toks).strip(" -:|*_.,;"))

    # Пост-очистка склеенных названий: "пампушками Салат..." -> "Салат..."
    low2 = n.lower()
    anchor_pos: list[tuple[int, str]] = []
    for a in _SPLIT_ANCHORS:
        m = re.search(rf"\b{re.escape(a)}\b", low2)
        if m:
            anchor_pos.append((m.start(), a))
    anchor_pos.sort(key=lambda x: x[0])
    if anchor_pos:
        first_pos, first_anchor = anchor_pos[0]
        if first_pos > 0:
            prefix = n[:first_pos].strip()
            if prefix:
                p_words = prefix.split()
                if len(p_words) <= 4:
                    n = _clean_line(n[first_pos:])
        elif len(anchor_pos) >= 2:
            # "Пиво ... Говядина ..." -> предпочесть вторую якорную еду.
            second_pos, second_anchor = anchor_pos[1]
            if first_anchor in _BEVERAGE_ANCHORS and second_anchor not in _BEVERAGE_ANCHORS:
                n = _clean_line(n[second_pos:])

    return n[:180] if len(n) > 180 else n


def _is_name_continuation_fragment(line: str) -> bool:
    s = _clean_line(line)
    if not s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    # Typical OCR split tail: one short lowercase word (e.g. "баклажаном").
    parts = [p for p in re.split(r"\s+", s) if p]
    if len(parts) != 1:
        return False
    w = parts[0]
    if len(w) < 5:
        return False
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]+", w):
        return False
    return w[:1].islower()


def _is_lowercase_continuation_line(line: str) -> bool:
    """
    OCR-обрыв многословного хвоста названия:
    "имичурри и кремом из копченого мацони 990"
    """
    s = _clean_line(line)
    if not s:
        return False
    if not _has_letters(s):
        return False
    no_money = _line_name_without_money(s)
    if not no_money:
        return False
    if _looks_like_service(no_money):
        return False
    if re.match(r"^\s*\d+\)\s*", no_money):
        return False
    parts = [p for p in re.split(r"\s+", no_money) if p]
    if len(parts) < 2:
        return False
    first = parts[0]
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]+", first):
        return False
    return first[:1].islower()


def _is_upper_continuation_fragment(line: str) -> bool:
    s = _clean_line(line)
    if not s or any(ch.isdigit() for ch in s):
        return False
    parts = [p for p in re.split(r"\s+", s) if p]
    if len(parts) != 1:
        return False
    w = parts[0]
    if len(w) < 4:
        return False
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]+", w):
        return False
    return w.isupper()


def _is_short_suffix_word(line: str) -> bool:
    """
    Однословный хвост OCR (например "трески"), который логично
    приклеивать к предыдущей строке названия.
    """
    s = _clean_line(line)
    if not s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    parts = [p for p in re.split(r"\s+", s) if p]
    if len(parts) != 1:
        return False
    w = parts[0]
    if len(w) < 5:
        return False
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]+", w):
        return False
    return True


def _looks_truncated_prev_name(name: str) -> bool:
    """
    Признак OCR-обрыва в конце названия:
    короткий последний токен ("бакл", "глазир.") или хвост с дефисом.
    """
    s = _clean_line(name)
    if not s:
        return False
    parts = [p for p in re.split(r"\s+", s) if p]
    if not parts:
        return False
    last = parts[-1].strip(".,;:|")
    if not last:
        return False
    if last.endswith("-"):
        return True
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё]{2,4}", last):
        return True
    return False


def _best_pending_name(parts: List[str], *, guest_mode: bool = False) -> str:
    # Берём ближайшую к цене осмысленную строку, чтобы не склеивать шапки.
    if guest_mode:
        picked: List[str] = []
        for p in reversed(parts):
            c = _clean_line(p)
            if not c or _looks_like_service(c) or _is_pure_money_line(c):
                if picked:
                    break
                continue
            if _is_plausible_item_name(c):
                picked.append(c)
                if len(picked) >= 3:
                    break
        if picked:
            return _join_name(list(reversed(picked)))

    for idx in range(len(parts) - 1, -1, -1):
        p = parts[idx]
        c = _clean_line(p)
        if not c:
            continue
        if _is_pure_money_line(c):
            continue
        if _looks_like_service(c):
            continue
        if _is_plausible_item_name(c):
            # OCR часто рвёт название на 2 строки:
            # "Паштет из печени" + "трески" => объединяем.
            if _is_short_suffix_word(c) and idx > 0:
                prev = _clean_line(parts[idx - 1])
                if prev and _is_plausible_item_name(prev) and not _is_pure_money_line(prev) and not _looks_like_service(prev):
                    return _clean_line(f"{prev} {c}")
            return c
    return _join_name(parts)


def _first_pending_name(parts: List[str]) -> str:
    for p in parts:
        c = _clean_line(p)
        if not c:
            continue
        if _is_pure_money_line(c) or _looks_like_service(c):
            continue
        if _is_plausible_item_name(c):
            return c
    return ""


def _pick_amount_from_line(line: str, *, decimal_table: bool, mode: str) -> Optional[int]:
    toks = _parse_money_tokens(line)
    if not toks:
        return None
    no_letters = not _has_letters(line)
    pure_money = _is_pure_money_line(line) or (no_letters and len(toks) >= 2)
    low = (line or "").lower()
    stripped = (line or "").strip()

    # Отрицательные строки в таблицах обычно скидки/коррекции, не товарные цены.
    if decimal_table and ("-" in line) and no_letters:
        return None

    # Qty placeholders like "1." / "2." / "1" are common in чековых колонках.
    if re.fullmatch(r"\d+\.?", stripped):
        try:
            qv = int(stripped.rstrip("."))
        except Exception:
            qv = -1
        if 0 <= qv <= 20:
            return None

    # Pure-money qty placeholders like "1." / "2" are not prices.
    if pure_money and len(toks) == 1:
        v1, h1, _ = toks[0]
        if (not h1) and v1 <= 20 and re.fullmatch(r"\d+\.?", stripped):
            return None

    # Pure-money вида "2 500.00" / "1 370,00" чаще означает qty + price.
    m_qty_price = re.fullmatch(r"\s*(\d{1,2})\s+(\d{2,4}[\,\.]\d{2})\s*", line)
    if m_qty_price:
        qty = int(m_qty_price.group(1))
        if 1 <= qty <= 20:
            raw_price = m_qty_price.group(2).replace(",", ".")
            try:
                return int(round(float(raw_price)))
            except Exception:
                pass

    # Quantity placeholders like "1..." / "2..." are not prices.
    if re.search(r"\b\d+\s*\.\.\.", low):
        return None
    # Qty x unit = sum (e.g. "10 x 60,00 = 600,00"): берём правую сумму.
    m_mul = re.search(
        r"(\d+(?:[\,\.]\d+)?)\s*[xх*]\s*(\d+(?:[\,\.]\d+)?)\s*=\s*(\d+(?:[\,\.]\d+)?)",
        low,
    )
    if m_mul:
        raw_sum = m_mul.group(3).replace(",", ".")
        try:
            return int(round(float(raw_sum)))
        except Exception:
            pass
    # Quantity like "12,500" (литры/шоты) should not be treated as price.
    if re.fullmatch(r"\s*\d{1,2}[\,\.]\d{3}\s*", low):
        return None

    # Volumetric/quantity tokens like "12,500" near ml/l are usually not item price.
    if _has_volume_hint(low) and re.search(r"\b\d{1,2}[,\.]\d{3}\b", low):
        return None
    # В строках-названиях вида '"... 50г' число обычно граммовка, а не цена.
    if (
        (not pure_money)
        and len(toks) == 1
        and toks[0][0] <= 200
        and (
            re.search(r"\b(г|гр|мл|ml|л)\b", low)
            or re.search(r"\d+\s*(г|гр|мл|ml|л)\b", low)
        )
        and ("=" not in low)
        and (" x " not in low)
        and (" х " not in low)
    ):
        return None

    # 1) Частый случай "цена количество сумма": берём сумму.
    if len(toks) >= 3:
        v1, _, _ = toks[-3]
        v2, _, _ = toks[-2]
        v3, h3, _ = toks[-1]
        if 0 < v2 <= 20 and abs((v1 * v2) - v3) <= max(0.05 * v3, 0.5):
            return _to_amount(v3, has_decimals=h3, mode=mode)

    # Для табличных mixed-строк часто формат: "Название ... qty сумма".
    # В этом случае берём последний денежный токен как сумму позиции.
    if decimal_table and not pure_money:
        if _has_letters(line) and len(toks) >= 2:
            v_last, h_last, _ = toks[-1]
            amount = _to_amount(v_last, has_decimals=h_last, mode=mode)
            if amount >= 50:
                return amount
        return None

    # Для табличных pure-money строк чаще всего последняя цифра — сумма позиции.
    if decimal_table and pure_money and len(toks) >= 1:
        if len(toks) == 1:
            v1, h1, _ = toks[0]
            # В table-mode одиночные small числа почти всегда qty, а не цена.
            if v1 <= 20:
                return None
            if (not h1) and v1 <= 30:
                return None
        val, has_dec, _ = toks[-1]
        return _to_amount(val, has_decimals=has_dec, mode=mode)

    # Pure-money с маленьким числом чаще quantity (1 / 2 / 5 / 12,500), а не цена.
    if pure_money and len(toks) == 1:
        v1, h1, d1 = toks[0]
        if v1 <= 20:
            return None
        if h1 and v1 <= 30:
            return None
        # OCR вида 12,500 в колонке количества.
        if h1 and d1 is not None and len(d1) >= 3:
            return None

    # 2) Строки вида "5.50 1.00" => цена * кол-во.
    if len(toks) == 2 and pure_money:
        v1, h1, _ = toks[0]
        v2, h2, _ = toks[1]
        if h1 and h2 and 0 < v2 <= 20:
            total = v1 * v2
            return _to_amount(total, has_decimals=True, mode=mode)

    # 3) Явный quantity без цены (например "1 Сет ...", "2 Кимчи ...").
    if len(toks) == 1 and not pure_money:
        v1, h1, _ = toks[0]
        if (not h1) and v1 <= 20 and _has_letters(line):
            return None

    # "500 м" / "500 m" в конце обычно обрыв "500 мл", это объём, не цена.
    if len(toks) == 1 and re.search(r"\b\d{2,4}\s*[mм]\s*$", low):
        return None

    # 4) В строках с "мл/л/шт" без цены часто встречается только объём.
    if _has_volume_hint(low) or re.search(r"\bшт\b", low):
        max_tok = max(v for v, _, _ in toks)
        if max_tok <= 80 and all((not h) for _, h, _ in toks):
            return None

    # В mixed line у цены чаще всего есть дробная часть, берём последний такой токен.
    for val, has_dec, _ in reversed(toks):
        if has_dec:
            return _to_amount(val, has_decimals=has_dec, mode=mode)

    # Иначе для RUB обычно лучше самый большой токен (кроме qty/объёмов).
    if mode == "rub":
        val, has_dec, _ = max(toks, key=lambda t: t[0])
        return _to_amount(val, has_decimals=has_dec, mode=mode)

    val, has_dec, _ = toks[-1]
    return _to_amount(val, has_decimals=has_dec, mode=mode)


def _line_name_without_money(line: str) -> str:
    name = _MONEY_TOKEN.sub(" ", line)
    # Remove OCR arithmetic garbage from qty*price rows.
    name = re.sub(r"[\*\=\(\)\[\]<>]+", " ", name)
    name = _clean_line(name).strip(" -:|*_.;,/")
    if not _has_letters(name):
        return ""
    return name


def _extract_negative_amount(line: str, *, mode: str) -> Optional[int]:
    m = re.search(r"-\s*(\d{1,3}(?:[ \u00A0]\d{3})*|\d+)(?:[\,\.](\d{1,2}))?", line)
    if not m:
        return None
    int_part = m.group(1).replace(" ", "").replace("\u00A0", "")
    dec = m.group(2)
    try:
        if dec is None:
            val = float(int(int_part))
            return -_to_amount(val, has_decimals=False, mode=mode)
        dec2 = (dec + "0")[:2]
        val = float(f"{int_part}.{dec2}")
        return -_to_amount(val, has_decimals=True, mode=mode)
    except Exception:
        return None


_ALCOHOL_HINTS = (
    "водка", "вино", "виски", "ром", "джин", "ликер", "коньяк", "бренди",
    "текила", "шампан", "сидр", "пиво", "настойка",
)
_SOFT_DRINK_HINTS = (
    "чай", "нарзан", "бонаква", "cola", "кока", "фанта", "лимонад",
    "морс", "сок", "вода",
)


def _is_alcohol_item(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in _ALCOHOL_HINTS)


def _is_soft_drink_item(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in _SOFT_DRINK_HINTS)


def _merge_same_alcohol_items(items: List[ReceiptItem]) -> List[ReceiptItem]:
    acc: dict[str, int] = {}
    order: List[str] = []
    others: List[ReceiptItem] = []
    for it in items:
        nm = _clean_line(it.name)
        if _is_alcohol_item(nm):
            key = re.sub(r"\s+", " ", nm.lower()).strip()
            if key not in acc:
                acc[key] = 0
                order.append(key)
            acc[key] += int(it.price)
        else:
            others.append(it)
    merged = [ReceiptItem(name=next((o.name for o in items if re.sub(r"\s+", " ", o.name.lower()).strip() == k), k), price=acc[k], meta=None) for k in order]
    return others + merged


def _repair_suspicious_soft_drink_prices(items: List[ReceiptItem], total: int | None) -> List[ReceiptItem]:
    """
    OCR иногда склеивает qty+price в одно число: 2 + 500 -> 2500, 1 + 370 -> 1370.
    Применяем repair только если текущий synthetic уже высокий.
    """
    if not total:
        return items
    s = sum(int(i.price) for i in items)
    if abs(total - s) / max(1, abs(int(total))) < 0.20:
        return items

    out: List[ReceiptItem] = []
    for it in items:
        price = int(it.price)
        nm = it.name or ""
        if (not _is_soft_drink_item(nm)) or _is_alcohol_item(nm):
            out.append(it)
            continue
        if 1000 <= price <= 9999:
            qty = price // 1000
            tail = price % 1000
            if 1 <= qty <= 9 and 70 <= tail <= 990:
                out.append(ReceiptItem(name=it.name, price=tail, meta=it.meta))
                continue
        out.append(it)
    return out


def _parse_receipt_text_rule(text: str) -> List[ReceiptItem]:
    if not isinstance(text, str):
        return []

    try:
        text, _ = normalize_ocr_text(text)
    except Exception:
        pass

    mode = _money_mode(text)
    kind = _receipt_kind(text)
    is_guest = (kind == "guest")
    is_decimal_table = (kind == "table")
    total = extract_total_rub(text)

    raw_lines = [_clean_line(l) for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in raw_lines if l]

    items: List[ReceiptItem] = []
    pending_parts: List[str] = []
    current_guest: Optional[str] = None
    skip_next_pure_money_after_total = False
    deferred_amount: Optional[int] = None
    deferred_age = 0
    forced_name_for_next_amount: Optional[str] = None

    def add_item(name: str, amount: int) -> bool:
        if amount <= 1:
            # OCR-шум: единичные "цены" в товарных строках.
            return False
        meta = {"guest": current_guest} if current_guest else None
        item_name = _cleanup_item_name(name or "Позиция без названия")
        if not _is_plausible_item_name(item_name):
            return False
        letters = re.findall(r"[A-Za-zА-Яа-яЁё]", item_name)
        if len(letters) <= 4 and amount >= 1000:
            # Короткие обрывки вроде "авок" с большой суммой почти всегда шум OCR.
            return False
        words = re.findall(r"[A-Za-zА-Яа-яЁё]+", item_name)
        if len(words) == 1 and len(words[0]) <= 4 and amount <= 300:
            # Короткие одиночные OCR-обрывки: "мл", "ро", "икубков" и т.п.
            return False
        if len("".join(words)) <= 4 and amount >= 300:
            return False
        if _is_condiment_like(item_name) and amount >= 1200:
            # "Аджика/соус/хлеб" не должны стоить как секционный subtotal.
            return False
        if meta and meta.get("guest"):
            item_name = f"[g{meta['guest']}] {item_name}"
        items.append(ReceiptItem(name=item_name, price=amount, meta=meta))
        return True

    parse_start = 0
    if is_decimal_table:
        for i, line in enumerate(lines):
            low = line.lower()
            if ("блюдо" in low or "наименование" in low or "товар" in low):
                win = " ".join(lines[max(0, i - 2): min(len(lines), i + 3)]).lower()
                if ("кол-во" in win or "колво" in win or "кол-в" in win) and ("сумма" in win):
                    parse_start = i + 1
                    break

    paired_amount_by_idx: dict[int, int] = {}
    consumed_amount_idx: set[int] = set()
    if is_decimal_table:
        for i in range(parse_start, len(lines) - 1):
            line = lines[i]
            if not line or _is_pure_money_line(line) or _looks_like_service(line):
                continue
            if not _has_letters(line):
                continue
            j = i + 1
            nxt = lines[j]
            if not nxt:
                continue
            # Pairing only for compact qty+sum rows like "1,00 440,00".
            if _has_letters(nxt):
                continue
            if len(_parse_money_tokens(nxt)) < 2:
                continue
            a = _pick_amount_from_line(nxt, decimal_table=is_decimal_table, mode=mode)
            if a is None or a <= 0:
                continue
            # отсекаем qty-строки и явные subtotal/total
            if a < 50:
                continue
            if total is not None and a > int(total * 1.1):
                continue
            paired_amount_by_idx[i] = a
            consumed_amount_idx.add(j)
            continue

            # unreachable marker for readability

        # Name + qty + amount pattern:
        # "Настойка ...", "8.00", "1680.00" -> amount belongs to name line.
        for i in range(parse_start, len(lines) - 2):
            line = lines[i]
            if not line or _is_pure_money_line(line) or _looks_like_service(line):
                continue
            if not _has_letters(line):
                continue
            q_line = lines[i + 1]
            sum_line = lines[i + 2]
            if not q_line or not sum_line:
                continue
            if not _is_pure_money_line(q_line):
                continue
            q_toks = _parse_money_tokens(q_line)
            if len(q_toks) != 1:
                continue
            q_val, _, _ = q_toks[0]
            if not (0 < q_val <= 20):
                continue
            if not _is_pure_money_line(sum_line):
                continue
            a = _pick_amount_from_line(sum_line, decimal_table=is_decimal_table, mode=mode)
            if a is None or a < 50:
                continue
            if total is not None and a > int(total * 1.1):
                continue
            # Если сразу после суммы идёт "Итого/Всего", это секционный subtotal.
            if (i + 3) < len(lines) and (_has_total_marker(lines[i + 3]) or _GUEST_SUBTOTAL_RE.search(lines[i + 3])):
                continue
            if i in paired_amount_by_idx:
                continue
            paired_amount_by_idx[i] = a
            consumed_amount_idx.add(i + 1)
            consumed_amount_idx.add(i + 2)

    for i in range(parse_start, len(lines)):
        if i in consumed_amount_idx:
            continue
        line = lines[i]
        low = line.lower()

        # Цена без валидного имени живёт недолго и приклеивается к
        # следующему осмысленному названию блюда/напитка.
        if deferred_amount is not None:
            deferred_age += 1
            if deferred_age > 3:
                deferred_amount = None
                deferred_age = 0

        # Track guest blocks
        if is_guest:
            m_guest = _GUEST_HEADER_RE.search(line)
            if m_guest and not _GUEST_SUBTOTAL_RE.search(line):
                gnum = int(m_guest.group(1))
                if not (1 <= gnum <= 20):
                    continue
                current_guest = str(gnum)
                pending_parts = []
                continue

        if _has_total_marker(line) or _GUEST_SUBTOTAL_RE.search(line):
            pending_parts = []
            skip_next_pure_money_after_total = True
            continue

        if _looks_like_service(line) and not _is_pure_money_line(line):
            pending_parts = []
            continue

        neg_amount = _extract_negative_amount(line, mode=mode)
        if neg_amount is not None:
            next_low = (lines[i + 1].lower() if (i + 1) < len(lines) else "")
            if ("скидк" in low) or ("скидк" in next_low):
                if items:
                    prev = items[-1]
                    # Применяем скидку к последней позиции только в правдоподобном диапазоне.
                    if abs(neg_amount) <= max(30, int(prev.price * 0.6)):
                        new_price = max(0, int(prev.price) + int(neg_amount))
                        items[-1] = ReceiptItem(name=prev.name, price=new_price, meta=prev.meta)
                pending_parts = []
                deferred_amount = None
                deferred_age = 0
                continue

        amount = paired_amount_by_idx.get(i)
        if is_decimal_table and amount is not None:
            if add_item(line, amount):
                pending_parts = []
                deferred_amount = None
                deferred_age = 0
                continue
        if amount is None:
            amount = _pick_amount_from_line(line, decimal_table=is_decimal_table, mode=mode)
        if amount is None:
            recovered_missing_sum = False
            if is_decimal_table and _has_letters(line):
                # OCR-обрыв перед итогом: "Название", "qty", затем "Всего/Итого" без строки суммы.
                q_line = lines[i + 1] if (i + 1) < len(lines) else ""
                after_q = lines[i + 2] if (i + 2) < len(lines) else ""
                if q_line and _is_pure_money_line(q_line):
                    q_toks = _parse_money_tokens(q_line)
                    if len(q_toks) == 1 and 0 < q_toks[0][0] <= 20:
                        if _has_total_marker(after_q) or _GUEST_SUBTOTAL_RE.search(after_q):
                            key = _compact(_cleanup_item_name(line))
                            if key:
                                for prev_it in reversed(items):
                                    if _compact(prev_it.name) == key and int(prev_it.price) > 0:
                                        if add_item(line, int(prev_it.price)):
                                            pending_parts = []
                                            deferred_amount = None
                                            deferred_age = 0
                                            recovered_missing_sum = True
                                            break
            if recovered_missing_sum:
                continue
            # Хвост без цены: приклеиваем к предыдущей позиции.
            if _is_name_continuation_fragment(line) and items:
                last = items[-1]
                items[-1] = ReceiptItem(
                    name=_cleanup_item_name(f"{last.name} {line}"),
                    price=last.price,
                    meta=last.meta,
                )
                pending_parts = []
                continue
            if deferred_amount is not None and not _looks_like_service(line) and not _is_pure_money_line(line):
                if _is_name_continuation_fragment(line) and items:
                    last = items[-1]
                    items[-1] = ReceiptItem(
                        name=_cleanup_item_name(f"{last.name} {line}"),
                        price=last.price,
                        meta=last.meta,
                    )
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
                if _is_upper_continuation_fragment(line) and items:
                    last = items[-1]
                    items[-1] = ReceiptItem(
                        name=_cleanup_item_name(f"{last.name} {line}"),
                        price=last.price,
                        meta=last.meta,
                    )
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
                if add_item(line, deferred_amount):
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
            if line and not _looks_like_service(line):
                pending_parts.append(line)
            continue

        if amount <= 0:
            if deferred_amount is not None and not _looks_like_service(line) and not _is_pure_money_line(line):
                if _is_name_continuation_fragment(line) and items:
                    last = items[-1]
                    items[-1] = ReceiptItem(
                        name=_cleanup_item_name(f"{last.name} {line}"),
                        price=last.price,
                        meta=last.meta,
                    )
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
                if _is_upper_continuation_fragment(line) and items:
                    last = items[-1]
                    items[-1] = ReceiptItem(
                        name=_cleanup_item_name(f"{last.name} {line}"),
                        price=last.price,
                        meta=last.meta,
                    )
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
                if add_item(line, deferred_amount):
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
            if (not _is_pure_money_line(line)) and _has_letters(line) and (not _looks_like_service(line)):
                pending_parts.append(line)
            continue

        # "Цена=1" в товарной строке почти всегда OCR-шум.
        if amount <= 1:
            if _is_name_continuation_fragment(line) and items:
                last = items[-1]
                items[-1] = ReceiptItem(
                    name=_cleanup_item_name(f"{last.name} {line}"),
                    price=last.price,
                    meta=last.meta,
                )
            elif (not _looks_like_service(line)) and (not _is_pure_money_line(line)):
                pending_parts.append(_line_name_without_money(line) or line)
            continue

        # OCR-обрыв: многословный хвост с ценой в той же строке.
        if _is_lowercase_continuation_line(line) and items and _looks_truncated_prev_name(items[-1].name):
            prev = items[-1]
            merged_name = _cleanup_item_name(f"{prev.name} {_line_name_without_money(line)}")
            merged_price = max(int(prev.price), int(amount))
            items[-1] = ReceiptItem(name=merged_name, price=merged_price, meta=prev.meta)
            pending_parts = []
            deferred_amount = None
            deferred_age = 0
            continue

        # При отсутствии total очень крупная "цена" обычно OCR-мусор из шапки/адреса.
        if total is None and amount >= 50_000:
            pending_parts = []
            deferred_amount = None
            deferred_age = 0
            continue
        if amount > 500_000:
            continue

        if total is not None and amount > int(total * 1.1):
            # subtotal/чужие суммы не должны стать позициями.
            continue

        if (not _is_pure_money_line(line)) and amount < 70 and (_has_volume_hint(low) or re.search(r"\bшт\b", low)):
            continue

        # После строки "Итого" обычно идёт pure-money итог — не считаем его товаром.
        if skip_next_pure_money_after_total and _is_pure_money_line(line):
            skip_next_pure_money_after_total = False
            continue
        skip_next_pure_money_after_total = False

        # Для защиты от ID/дат: сильно больше итоговой суммы обычно мусор.
        if total is not None and amount > int(total * 1.25) and not _is_pure_money_line(line):
            continue

        # OCR иногда ставит сумму чека перед "Всего/Итого" — не считаем это товаром.
        if total is not None and amount == total and _is_pure_money_line(line):
            near_total_marker = False
            for j in range(i + 1, min(len(lines), i + 4)):
                if _has_total_marker(lines[j]) or _GUEST_SUBTOTAL_RE.search(lines[j]):
                    near_total_marker = True
                    break
            if near_total_marker:
                pending_parts = []
                continue
        if _is_pure_money_line(line) or is_decimal_table:
            # OCR-обрыв: строка-хвост получила цену отдельно.
            # Склеиваем с предыдущей позицией и принимаем новую цену как более вероятную.
            if _is_name_continuation_fragment(line) and items and _looks_truncated_prev_name(items[-1].name):
                prev = items[-1]
                merged_name = _cleanup_item_name(f"{prev.name} {line}")
                merged_price = max(int(prev.price), int(amount))
                items[-1] = ReceiptItem(name=merged_name, price=merged_price, meta=prev.meta)
                pending_parts = []
                deferred_amount = None
                deferred_age = 0
                continue
            # Pure-money равная total почти всегда итог чека, а не товар.
            if total is not None and amount == total and _is_pure_money_line(line):
                pending_parts = []
                continue
            if forced_name_for_next_amount:
                if add_item(forced_name_for_next_amount, amount):
                    forced_name_for_next_amount = None
                    deferred_amount = None
                    deferred_age = 0
                    pending_parts = []
                    continue
                forced_name_for_next_amount = None

            if pending_parts:
                name = _best_pending_name(pending_parts, guest_mode=is_guest)
                first_name = _first_pending_name(pending_parts)
                # subtotal-паттерн: qty-строка + сумма + "Итого ...".
                if _is_pure_money_line(line):
                    prev = lines[i - 1] if i > 0 else ""
                    near_total_marker = False
                    for j in range(i + 1, min(len(lines), i + 4)):
                        if _has_total_marker(lines[j]) or _GUEST_SUBTOTAL_RE.search(lines[j]):
                            near_total_marker = True
                            break
                    if near_total_marker and _is_pure_money_line(prev):
                        prev_toks = _parse_money_tokens(prev)
                        if len(prev_toks) == 1 and 0 < prev_toks[0][0] <= 20:
                            if _has_explicit_volume_unit(name or ""):
                                pending_parts = []
                                continue
                # OCR иногда кладёт подряд "напиток + блюдо", а потом 2 цены.
                # Для такого паттерна более дешёвую цену привяжем к напитку,
                # а вторую — к следующему блюду.
                if (
                    first_name
                    and name
                    and first_name != name
                    and (_is_soft_drink_item(first_name) or _is_alcohol_item(first_name))
                    and 70 <= amount <= 700
                ):
                    next_amount: Optional[int] = None
                    for j in range(i + 1, min(len(lines), i + 4)):
                        ln2 = lines[j]
                        if not ln2:
                            continue
                        a2 = _pick_amount_from_line(ln2, decimal_table=is_decimal_table, mode=mode)
                        if a2 and a2 > 0:
                            next_amount = a2
                            break
                        if not _is_pure_money_line(ln2):
                            break
                    if next_amount is not None and (amount + 80) <= next_amount <= 5000:
                        pending_parts = []
                        if add_item(first_name, amount):
                            forced_name_for_next_amount = name
                            deferred_amount = None
                            deferred_age = 0
                            continue
                pending_parts = []
                if not name or _looks_like_service(name):
                    deferred_amount = amount
                    deferred_age = 0
                    continue
                if not add_item(name, amount):
                    deferred_amount = amount
                    deferred_age = 0
                    continue
                deferred_amount = None
                deferred_age = 0
                continue

            prev = lines[i - 1] if i > 0 else ""
            if prev and not _looks_like_service(prev) and not _is_pure_money_line(prev):
                # Если предыдущая строка уже только что получила цену, а следом идёт
                # новое осмысленное название, текущую цену относим к следующей позиции.
                # Это снижает дубль-паттерн вида "Пивной хлеб 250/420".
                next_name_line = lines[i + 1] if (i + 1) < len(lines) else ""
                if (
                    items
                    and _compact(items[-1].name) == _compact(prev)
                    and next_name_line
                    and (not _is_pure_money_line(next_name_line))
                    and (not _looks_like_service(next_name_line))
                    and _is_plausible_item_name(next_name_line)
                ):
                    deferred_amount = amount
                    deferred_age = 0
                    continue
                if not add_item(prev, amount):
                    deferred_amount = amount
                    deferred_age = 0
                    continue
                deferred_amount = None
                deferred_age = 0
            else:
                deferred_amount = amount
                deferred_age = 0
            continue

        name_candidate = _line_name_without_money(line)
        if not name_candidate and pending_parts:
            name_candidate = _join_name(pending_parts)
            pending_parts = []

        if not name_candidate or _looks_like_service(name_candidate):
            continue

        if not add_item(name_candidate, amount):
            deferred_amount = amount
            deferred_age = 0
            continue
        deferred_amount = None
        deferred_age = 0
        pending_parts = []

    # Уберём явный дубль total как товар.
    if total is not None:
        filtered: List[ReceiptItem] = []
        for it in items:
            c = _compact(it.name)
            if it.price == total and ("позициябезназвания" in c or "итого" in c or "всего" in c):
                continue
            filtered.append(it)
        items = filtered

    # Финальная защита: удаляем мусорные позиции с ценой 0/1.
    items = [it for it in items if int(it.price) > 1]

    # Пост-обработка: сохраняем позиции раздельными как в чеке
    # (без агрессивного объединения одинакового алкоголя),
    # и только чиним типичные qty+price склейки.
    items = _repair_suspicious_soft_drink_prices(items, total)

    # Убираем дубли подряд (частая ошибка table-OCR: цена+название дублируются строками).
    deduped: List[ReceiptItem] = []
    for it in items:
        if deduped:
            prev = deduped[-1]
            if _compact(prev.name) == _compact(it.name) and int(prev.price) == int(it.price):
                nm = _compact(it.name)
                # Дедупим только явные OCR-дубли, чтобы не терять реальные повторы позиций.
                if ("позициябезназвания" in nm) or (len(nm) <= 6):
                    continue
            # OCR-обрыв: "Шашлык ... 1071" + "КОРЕЙКИ 1071" -> склеиваем в одну позицию.
            if int(prev.price) == int(it.price) and _is_upper_continuation_fragment(it.name):
                merged = _cleanup_item_name(f"{prev.name} {it.name}")
                deduped[-1] = ReceiptItem(name=merged, price=prev.price, meta=prev.meta)
                continue
        deduped.append(it)
    items = deduped

    # Если рядом лежит явный дубль (same name + same price), удаляем только тогда,
    # когда это улучшает сходимость к total.
    if total is not None and items:
        changed = True
        while changed:
            changed = False
            s_cur = sum(int(x.price) for x in items)
            diff_cur = abs(int(total) - s_cur)
            if diff_cur == 0:
                break
            for idx in range(1, len(items)):
                a = items[idx - 1]
                b = items[idx]
                if int(a.price) <= 0 or int(b.price) <= 0:
                    continue
                if _compact(a.name) != _compact(b.name):
                    continue
                if int(a.price) != int(b.price):
                    continue
                s_new = s_cur - int(b.price)
                diff_new = abs(int(total) - s_new)
                if diff_new < diff_cur:
                    items = items[:idx] + items[idx + 1:]
                    changed = True
                    break

    # Всегда сводим до итога чека, чтобы финальные расчёты были консистентны.
    if total is not None:
        s = sum(it.price for it in items)
        diff = total - s
        if diff != 0:
            items.append(ReceiptItem(name="Корректировка по итогу (synthetic)", price=diff, meta=None))

    return items


_PRECHECK_NUM_RE = re.compile(r"^\s*(\d{1,2})\)\s+(.+)$")
_PRECHECK_EQ_SUM_RE = re.compile(r"=\s*(\d{1,3}(?:[ \u00A0]\d{3})+|\d+)(?:[\,\.](\d{2}))?")


def _parse_precheck_numbered_eq(text: str) -> List[ReceiptItem]:
    """
    Узкий парсер precheck-формата:
      N) <name>
      ... (переносы)
      =<sum>,00 Руб.
    """
    lines = [_clean_line(l) for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if l]

    items: List[ReceiptItem] = []
    current_parts: List[str] = []

    def _flush_with_amount(amount: int) -> None:
        nonlocal current_parts
        if amount <= 1:
            current_parts = []
            return
        if amount > 500_000:
            current_parts = []
            return
        name = _cleanup_item_name(" ".join(current_parts) if current_parts else "")
        current_parts = []
        if not name:
            return
        if _looks_like_service(name):
            return
        if not _is_plausible_item_name(name):
            return
        items.append(ReceiptItem(name=name, price=int(amount), meta=None))

    for line in lines:
        if _has_total_marker(line):
            # precheck секция закончилась
            break

        m_num = _PRECHECK_NUM_RE.match(line)
        if m_num:
            base_name = _clean_line(m_num.group(2))
            if _looks_like_service(base_name):
                current_parts = []
                continue
            current_parts = [base_name]
            continue

        if not current_parts:
            continue

        m_eq = _PRECHECK_EQ_SUM_RE.search(line)
        if m_eq:
            int_part = m_eq.group(1).replace(" ", "").replace("\u00A0", "")
            dec = m_eq.group(2)
            try:
                if dec is None:
                    val = float(int(int_part))
                else:
                    val = float(f"{int_part}.{(dec + '0')[:2]}")
                _flush_with_amount(int(round(val)))
            except Exception:
                current_parts = []
            continue

        low = line.lower()
        # qty/arithmetic шум вроде "230,00 * 5,000 порц" не должен попадать в имя
        if ("порц" in low or "руб" in low) and any(ch.isdigit() for ch in low):
            continue
        if _is_pure_money_line(line):
            continue
        if _looks_like_service(line):
            continue

        tail = _line_name_without_money(line) or line
        tail = _clean_line(tail)
        if tail and _has_letters(tail):
            current_parts.append(tail)

    return items


def _synthetic_adjustment(items: List[ReceiptItem]) -> int:
    total = 0
    for it in items:
        if "корректировка по итогу" in (it.name or "").lower():
            total += int(it.price)
    return total


def parse_receipt_text_with_variant(text: str) -> tuple[List[ReceiptItem], str]:
    if not isinstance(text, str):
        return [], "rule_v1"

    base_items = _parse_receipt_text_rule(text)
    total = extract_total_rub(text)

    pre_items = _parse_precheck_numbered_eq(text)
    if not pre_items:
        return base_items, "rule_v1"

    if len(pre_items) < 8:
        return base_items, "rule_v1"

    service_cnt = sum(1 for it in pre_items if _looks_like_service(it.name))
    service_ratio = service_cnt / max(1, len(pre_items))
    if service_ratio > 0.15:
        return base_items, "rule_v1"

    if total is not None:
        pre_sum = sum(int(i.price) for i in pre_items)
        pre_synth_ratio = abs(int(total) - pre_sum) / max(1, abs(int(total)))
        base_synth_ratio = abs(_synthetic_adjustment(base_items)) / max(1, abs(int(total)))

        # Используем precheck только при не худшем качестве относительно baseline.
        if not (
            pre_synth_ratio + 0.02 < base_synth_ratio
            or (pre_synth_ratio <= base_synth_ratio + 0.01 and len(pre_items) >= len(base_items))
        ):
            return base_items, "rule_v1"

        if pre_synth_ratio > 0.10:
            return base_items, "rule_v1"

        diff = int(total) - pre_sum
        if diff != 0:
            pre_items = list(pre_items)
            pre_items.append(ReceiptItem(name="Корректировка по итогу (synthetic)", price=diff, meta=None))

    return pre_items, "rule_v1_precheck_eq"


def parse_receipt_text(text: str) -> List[ReceiptItem]:
    items, _ = parse_receipt_text_with_variant(text)
    return items
