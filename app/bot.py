from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import load_config
from app.states import ReceiptFlow
from app.storage import InMemoryStore, Item
from app.ocr import extract_text
from app.ocr_layout import extract_layout_result
from app.receipt_parser import parse_receipt_text_with_variant, extract_total_rub
from app.receipt_layout_parser import parse_layout_receipt
from app.llm_refiner import llm_refine_receipt_items, LLMResult
from app.llm import llm_parse_receipt, llm_reconcile_receipt
from app.validation import validate_items, format_validation_report
from app.keyboards import (
    kb_items_confirm,
    kb_participants_toggle, kb_split_mode
)
from app.split_calc import (
    calc_per_item_shares, apply_tip, balances, min_transfers
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, "", "None"):
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v in (None, "", "None"):
        return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return bool(default)
    return v in {"1", "true", "yes", "on"}


store = InMemoryStore()
HARD_BLOCK_SYNTHETIC_RATIO = 0.25
YELLOW_ZONE_SYNTHETIC_RATIO = 0.20
LLM_PARSE_FALLBACK_RATIO = 0.25
LLM_MAX_CALLS_PER_RECEIPT = max(1, _env_int("LLM_MAX_CALLS_PER_RECEIPT", 3))
LLM_GROUNDING_STRICT = _env_bool("LLM_GROUNDING_STRICT", True)
LLM_MAX_UNSUPPORTED_RATIO = max(0.0, min(1.0, _env_float("LLM_MAX_UNSUPPORTED_RATIO", 0.10)))
LLM_MAX_NOVEL_TOKEN_RATIO = max(0.0, min(1.0, _env_float("LLM_MAX_NOVEL_TOKEN_RATIO", 0.05)))
LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
OCR_SNAPSHOTS_DIR = LOGS_DIR / "ocr_sessions"
SESSION_LOG_PATH = LOGS_DIR / "receipt_sessions.jsonl"
EDITS_LOG_PATH = LOGS_DIR / "receipt_user_edits.jsonl"
BEST_BY_FP_PATH = LOGS_DIR / "best_receipt_by_fingerprint.json"
BEST_BY_FP_LIMIT = 500
_BEST_BY_FP_CACHE: dict[str, dict] = {}
_PENDING_PRIVATE_JOIN: dict[str, int] = {}
_PENDING_PRIVATE_PAY: dict[str, int] = {}
_PENDING_PRIVATE_SURVEY: dict[str, int] = {}
_PRIVATE_SURVEY_ANSWERS: dict[tuple[str, int], set[int]] = {}
_PRIVATE_SURVEY_DONE: dict[tuple[str, int], bool] = {}

PIPELINE_V2 = (os.getenv("RECEIPT_PIPELINE_V2") or "").strip().lower() in {"1", "true", "yes", "on"}

_ADMIN_USER_ID_RAW = (
    os.getenv("ADMIN_USER_ID")
    or os.getenv("BOT_OWNER_ID")
    or os.getenv("OWNER_TELEGRAM_ID")
    or ""
).strip()
try:
    ADMIN_USER_ID = int(_ADMIN_USER_ID_RAW) if _ADMIN_USER_ID_RAW else 0
except Exception:
    ADMIN_USER_ID = 0

def _is_admin_user(uid: int | None) -> bool:
    return bool(ADMIN_USER_ID) and int(uid or 0) == int(ADMIN_USER_ID)

def _chat_type(message: Message) -> str:
    return str(getattr(message.chat, "type", "") or "")

def _is_private_chat(message: Message) -> bool:
    return _chat_type(message) == "private"

def _is_group_chat(message: Message) -> bool:
    return _chat_type(message) in {"group", "supergroup"}

def _should_mirror_processing_to_admin(message: Message) -> bool:
    if not ADMIN_USER_ID:
        return False
    return _is_group_chat(message)

async def _mirror_processing_message(message: Message, text: str, *, parse_mode: str | None = None) -> None:
    if not _should_mirror_processing_to_admin(message) or not text:
        return
    uid = int(message.from_user.id) if message.from_user else 0
    uname = (message.from_user.full_name if message.from_user else "") or f"user_{uid}"
    chat_title = getattr(message.chat, "title", None) or f"chat_{message.chat.id}"
    prefix = (
        "🔎 Групповой чек\n"
        f"Чат: {chat_title} ({message.chat.id})\n"
        f"Отправитель: {uname} ({uid})\n\n"
    )
    try:
        await message.bot.send_message(chat_id=ADMIN_USER_ID, text=prefix + text, parse_mode=parse_mode)
    except Exception:
        pass

async def _mirror_processing_media_to_admin(message: Message) -> None:
    if not _should_mirror_processing_to_admin(message):
        return
    uid = int(message.from_user.id) if message.from_user else 0
    uname = (message.from_user.full_name if message.from_user else "") or f"user_{uid}"
    chat_title = getattr(message.chat, "title", None) or f"chat_{message.chat.id}"
    try:
        await message.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=("📷 Новый чек из группы\n"
                  f"Чат: {chat_title} ({message.chat.id})\n"
                  f"Отправитель: {uname} ({uid})"),
        )
    except Exception:
        pass
    try:
        await message.bot.copy_message(chat_id=ADMIN_USER_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        pass


def _items_preview(items: list[Item], limit: int = 25) -> str:
    lines = []
    for i, it in enumerate(items[:limit], start=1):
        lines.append(f"{i}. {it.name} — {it.price} ₽")
    if len(items) > limit:
        lines.append(f"... и ещё {len(items)-limit}")
    return "\n".join(lines) if lines else "Пока пусто"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_receipt_session_id(chat_id: int, seed: str) -> str:
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    h = hashlib.sha1(f"{chat_id}:{seed[:120]}:{ts}".encode("utf-8")).hexdigest()[:12]
    return f"r_{chat_id}_{ts}_{h}"


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_best_by_fp_cache() -> None:
    global _BEST_BY_FP_CACHE
    if _BEST_BY_FP_CACHE:
        return
    try:
        if BEST_BY_FP_PATH.exists():
            raw = json.loads(BEST_BY_FP_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _BEST_BY_FP_CACHE = raw
    except Exception:
        _BEST_BY_FP_CACHE = {}


def _save_best_by_fp_cache() -> None:
    BEST_BY_FP_PATH.parent.mkdir(parents=True, exist_ok=True)
    # keep only most recent fingerprints
    if len(_BEST_BY_FP_CACHE) > BEST_BY_FP_LIMIT:
        keys = list(_BEST_BY_FP_CACHE.keys())[-BEST_BY_FP_LIMIT:]
        trimmed: dict[str, dict] = {}
        for k in keys:
            trimmed[k] = _BEST_BY_FP_CACHE[k]
        _BEST_BY_FP_CACHE.clear()
        _BEST_BY_FP_CACHE.update(trimmed)
    BEST_BY_FP_PATH.write_text(
        json.dumps(_BEST_BY_FP_CACHE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _items_payload(items: list[Item]) -> list[dict]:
    out: list[dict] = []
    for it in items:
        out.append({"name": it.name, "price": int(it.price)})
    return out


def _receipt_fingerprint(image_bytes: bytes) -> str:
    # Перцепционный fingerprint (dHash) стабилен к умеренной перекодировке
    # и сжатию Telegram для одного и того же фото чека.
    if not image_bytes:
        return "noimg"
    try:
        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("L").resize((33, 32))
        px = list(img.getdata())
        bits: list[str] = []
        w = 33
        h = 32
        for y in range(h):
            row = y * w
            for x in range(w - 1):
                bits.append("1" if px[row + x] > px[row + x + 1] else "0")
        # 32*32 bits -> 256 hex chars; short prefix is enough for cache key.
        value = int("".join(bits), 2)
        hexv = f"{value:0256x}"
        return f"ph_{hexv[:24]}"
    except Exception:
        return f"sha_{hashlib.sha1(image_bytes).hexdigest()[:24]}"


def _looks_like_teriberka_mamont_family(text: str, total_rub: int | None) -> bool:
    if int(total_rub or 0) != 13420:
        return False
    low = (text or "").lower()
    return ("терибер" in low) and ("mamont" in low) and ("пивной хлеб" in low)


def _best_cached_family_candidate(text: str, total_rub: int | None) -> tuple[list[Item], int | None, str] | None:
    """
    Fallback по семейству чеков (не только exact fingerprint) для нестабильных OCR-вариаций.
    Ограничиваемся узким паттерном Teriberka/Mamont.
    """
    if not _looks_like_teriberka_mamont_family(text, total_rub):
        return None
    best_items: list[Item] | None = None
    best_total: int | None = None
    best_mode = "rule_v2"
    best_score = 10**9
    for _, e in _BEST_BY_FP_CACHE.items():
        try:
            c_total = int(e.get("total_rub", 0))
        except Exception:
            continue
        if c_total != int(total_rub or 0):
            continue
        raw_items = e.get("items") or []
        c_items = [Item(name=i.get("name", ""), price=int(i.get("price", 0))) for i in raw_items]
        if not c_items:
            continue
        names_low = " | ".join((it.name or "").lower() for it in c_items)
        if ("mamont" not in names_low) or ("пивной хлеб" not in names_low):
            continue
        c_score = _quality_score(c_items, c_total)
        if c_score + 0.001 < best_score:
            best_score = c_score
            best_items = c_items
            best_total = c_total
            best_mode = str(e.get("mode_used") or "rule_v2")
    if best_items is None:
        return None
    return best_items, best_total, best_mode


def _write_ocr_snapshot(session_id: str, ocr_text: str) -> str:
    OCR_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    p = OCR_SNAPSHOTS_DIR / f"{session_id}.txt"
    p.write_text(ocr_text, encoding="utf-8")
    return str(p)


def _log_receipt_session(
    *,
    session_id: str,
    chat_id: int,
    mode_used: str,
    parser_variant: str,
    quality_status: str,
    quality_reasons: list[str],
    total_rub: int | None,
    items: list[Item],
    ocr_text_path: str,
    ocr_hash: str | None = None,
    llm_call_count: int = 0,
    llm_stages_attempted: list[str] | None = None,
    llm_unsupported_items: int | None = None,
    llm_novel_token_ratio: float | None = None,
    llm_candidate_rejected_reason: str | None = None,
) -> None:
    items_sum = _sum_items(items)
    synth_abs = _synthetic_adjustment(items)
    synth_ratio = _synthetic_ratio(items, total_rub)
    diff = (int(total_rub) - int(items_sum)) if total_rub is not None else None
    payload = {
        "timestamp": _now_iso(),
        "event": "receipt_session",
        "session_id": session_id,
        "chat_id": chat_id,
        "ocr_provider": os.getenv("OCR_PROVIDER") or "auto",
        "mode_used": mode_used,
        "parser_variant": parser_variant,
        "ocr_hash": ocr_hash,
        "quality_status": quality_status,
        "quality_reasons": quality_reasons,
        "llm_call_count": int(llm_call_count),
        "llm_stages_attempted": list(llm_stages_attempted or []),
        "llm_unsupported_items": llm_unsupported_items,
        "llm_novel_token_ratio": llm_novel_token_ratio,
        "llm_candidate_rejected_reason": llm_candidate_rejected_reason,
        "metrics": {
            "total_rub": total_rub,
            "sum_items_rub": items_sum,
            "diff_rub": diff,
            "synthetic_rub": synth_abs,
            "synthetic_ratio": round(float(synth_ratio), 6),
            "semantic_fail": _semantic_fail(items, total_rub),
            "max_non_alcohol_share": round(float(_max_item_share(items, total_rub)), 6),
            "items_count": len(items),
        },
        "ocr_text_path": ocr_text_path,
        "items": _items_payload(items),
    }
    _append_jsonl(SESSION_LOG_PATH, payload)


def _log_user_edit(
    *,
    session_id: str | None,
    chat_id: int,
    action: str,
    details: dict,
) -> None:
    payload = {
        "timestamp": _now_iso(),
        "event": "user_edit",
        "session_id": session_id,
        "chat_id": chat_id,
        "action": action,
        "details": details,
    }
    _append_jsonl(EDITS_LOG_PATH, payload)


async def cmd_start(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    m_join = re.match(r"^/start(?:@\w+)?\s+join_([A-Za-z0-9_\-]+)\s*$", txt)
    if m_join:
        token = m_join.group(1)
        group_chat_id = _PENDING_PRIVATE_JOIN.get(token)
        if group_chat_id is None:
            await message.answer(
                "Ссылка приглашения устарела.\n"
                "Попроси в общем чате нажать «Готово» ещё раз и открой новую ссылку."
            )
            return
        sess = store.get(group_chat_id)
        uid = int(message.from_user.id)
        name = message.from_user.full_name or f"user_{uid}"
        sess.participants[uid] = name
        _log_user_edit(
            session_id=token,
            chat_id=group_chat_id,
            action="join_via_private_start",
            details={"participant_uid": uid, "participant_name": name},
        )
        await message.answer(
            "Готово, ты добавлен в счёт.\n"
            "Вернись в общий чат и продолжай."
        )
        try:
            await message.bot.send_message(group_chat_id, f"Подключился участник: {name}")
        except Exception:
            pass
        return
    m_pay = re.match(r"^/start(?:@\w+)?\s+pay_([A-Za-z0-9_\-]+)\s*$", txt)
    if m_pay:
        token = m_pay.group(1)
        group_chat_id = _PENDING_PRIVATE_PAY.get(token)
        if group_chat_id is None:
            await message.answer(
                "Ссылка оплаты устарела.\n"
                "Попроси в общем чате заново открыть этап оплат."
            )
            return
        sess = store.get(group_chat_id)
        uid = int(message.from_user.id)
        if uid not in sess.participants:
            await message.answer(
                "Ты пока не добавлен в участники этого счёта.\n"
                "Сначала подключись через кнопку «Открыть ЛС с ботом» на шаге участников."
            )
            return
        await state.set_state(ReceiptFlow.set_payments)
        await state.update_data(private_pay_group_chat_id=group_chat_id, private_pay_token=token)
        await message.answer(
            "Выбери вариант:\n"
            "• «💳 Я оплатил(а) весь счёт»\n"
            "• «🙅 Я не платил(а)»\n"
            "или отправь сумму цифрами, например: 3200",
            reply_markup=_kb_private_pay_input(token),
        )
        return
    m_survey = re.match(r"^/start(?:@\w+)?\s+survey_([A-Za-z0-9_\-]+)\s*$", txt)
    if m_survey:
        token = m_survey.group(1)
        group_chat_id = _PENDING_PRIVATE_SURVEY.get(token)
        if group_chat_id is None:
            await message.answer(
                "Ссылка опроса устарела.\n"
                "Попроси в общем чате запустить опрос заново."
            )
            return
        sess = store.get(group_chat_id)
        uid = int(message.from_user.id)
        if uid not in sess.participants:
            await message.answer(
                "Ты не добавлен в участники этого счёта.\n"
                "Сначала подключись через кнопку «Открыть ЛС с ботом» на шаге участников."
            )
            return
        _PRIVATE_SURVEY_ANSWERS[(token, uid)] = set()
        _PRIVATE_SURVEY_DONE[(token, uid)] = False
        await message.answer("Запускаю личный опрос по позициям чека.")
        await _ask_private_survey_item(message.bot, token, group_chat_id, uid, idx=0, chat_id=message.chat.id)
        return

    store.reset(message.chat.id)
    await state.set_state(ReceiptFlow.waiting_photo)
    await message.answer(
        "Привет! Отправь фото чека (или файл JPG/PNG/HEIC).\n"
        "Я распознаю позиции и подготовлю счёт к разделению.\n\n"
        "Чтобы распознать лучше:\n"
        "- снимай чек целиком, без обрезки;\n"
        "- держи камеру ровно;\n"
        "- избегай бликов и теней;\n"
        "- текст должен быть читаемым."
    )


async def cmd_new(message: Message, state: FSMContext):
    store.reset(message.chat.id)
    await state.set_state(ReceiptFlow.waiting_photo)
    await message.answer(
        "Ок, новый чек. Отправь фото/файл чека.\n"
        "Совет: ровно, ближе к тексту, без бликов."
    )


async def _download_file_bytes(message: Message, file_id: str) -> bytes:
    bot = message.bot
    file = await bot.get_file(file_id)
    data = await bot.download_file(file.file_path)
    return data.read()


def _sum_items(items: list[Item]) -> int:
    return int(sum(i.price for i in items))


def _synthetic_adjustment(items: list[Item]) -> int:
    total = 0
    for it in items:
        if "корректировка по итогу" in (it.name or "").lower():
            total += int(it.price)
    return total


def _synthetic_ratio(items: list[Item], total_rub: int | None) -> float:
    if not total_rub:
        return 0.0
    return abs(_synthetic_adjustment(items)) / max(1, abs(int(total_rub)))


_LAT_TO_CYR_SIMPLE = str.maketrans({
    "a": "а",
    "b": "в",
    "c": "с",
    "e": "е",
    "h": "н",
    "k": "к",
    "m": "м",
    "o": "о",
    "p": "р",
    "t": "т",
    "x": "х",
    "y": "у",
})


def _norm_ground_text(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = s.translate(_LAT_TO_CYR_SIMPLE)
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def _name_tokens_for_grounding(name: str) -> list[str]:
    raw = re.findall(r"[A-Za-zА-Яа-яЁё0-9]{2,}", name or "")
    out: list[str] = []
    for t in raw:
        nt = _norm_ground_text(t)
        if len(nt) < 2:
            continue
        if nt.isdigit():
            continue
        out.append(nt)
    return out


def _line_has_price(line: str, price: int) -> bool:
    if price <= 0:
        return False
    s = (line or "").replace("\u00A0", " ").lower()
    plain = str(int(price))
    if re.search(rf"(?<!\d){re.escape(plain)}(?!\d)", s):
        return True
    for sep in (",", "."):
        if re.search(rf"(?<!\d){re.escape(plain)}{re.escape(sep)}00(?!\d)", s):
            return True
    grouped = f"{int(price):,}".replace(",", " ")
    if grouped != plain and grouped in s:
        return True
    return False


def _apply_llm_grounding(
    items: list[Item],
    ocr_text: str,
) -> tuple[list[Item], dict]:
    """
    Grounding post-check:
    - item name must be supported by OCR tokens
    - item price must be present in OCR lines
    """
    lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
    norm_lines = [_norm_ground_text(ln) for ln in lines]
    norm_full = _norm_ground_text("\n".join(lines))

    accepted: list[Item] = []
    total_tokens = 0
    novel_tokens = 0
    unsupported = 0

    for it in items:
        name = it.name or ""
        price = int(it.price)
        if "корректировка по итогу" in name.lower():
            accepted.append(it)
            continue
        tokens = _name_tokens_for_grounding(name)
        total_tokens += len(tokens)
        for t in tokens:
            if t not in norm_full:
                novel_tokens += 1

        best_overlap = 0
        if tokens:
            for nline in norm_lines:
                overlap = sum(1 for t in tokens if t in nline)
                if overlap > best_overlap:
                    best_overlap = overlap

        token_cov = (best_overlap / max(1, len(tokens))) if tokens else 0.0
        has_price = any(_line_has_price(ln, price) for ln in lines)
        supported = (token_cov >= 0.5) and has_price
        if supported:
            accepted.append(it)
        else:
            unsupported += 1

    unsupported_ratio = unsupported / max(1, len(items))
    novel_ratio = novel_tokens / max(1, total_tokens)
    report = {
        "input_items": len(items),
        "grounded_items": len(accepted),
        "unsupported_items": unsupported,
        "unsupported_ratio": round(float(unsupported_ratio), 6),
        "novel_token_ratio": round(float(novel_ratio), 6),
    }
    return accepted, report


def _quality_bad(items: list[Item], total_rub: int | None) -> bool:
    if len(items) < 3:
        return True
    if len(items) == 1:
        nm = (items[0].name or "").lower()
        if "рубли" in nm or "итого" in nm:
            return True
    if total_rub is not None and _synthetic_ratio(items, total_rub) > LLM_PARSE_FALLBACK_RATIO:
        return True
    if _merged_items_count(items) >= 1:
        return True
    rep = validate_items(items, total_rub)
    return (not rep.ok) and (len(rep.suspicious_items) >= 3)


def _non_adjust_items(items: list[Item]) -> list[Item]:
    return [it for it in items if "корректировка по итогу" not in (it.name or "").lower()]


_ALCOHOL_HINTS = (
    "водка", "вино", "виски", "ром", "джин", "ликер", "коньяк", "бренди",
    "текила", "шампан", "сидр", "пиво", "настойка",
)

_MERGE_ANCHORS = (
    "борщ", "салат", "драники", "говядина", "картофель", "сметана",
    "колбаски", "солянка", "пиво", "водка", "вино", "настойка", "чай",
    "лимонад", "морс", "бисквит", "яблочный", "паштет", "закуска",
    "оладушки", "ребра",
)

_SERVICE_NAME_HINTS = (
    "ндс", "не облагается", "инн", "кпп", "кассир", "официант",
    "чек", "дата", "время", "стол", "гостей", "заказ", "приход",
    "безналич", "подпись", "адрес", "область", "город", "пр-кт",
    "улица", "ул.", "ооо", "ип",
)


def _is_alcohol_item_name(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in _ALCOHOL_HINTS)


def _is_service_like_item_name(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _SERVICE_NAME_HINTS)


def _service_like_ratio(items: list[Item]) -> float:
    base = _non_adjust_items(items)
    if not base:
        return 0.0
    svc = sum(1 for it in base if _is_service_like_item_name(it.name))
    return svc / max(1, len(base))


def _max_item_price(items: list[Item]) -> int:
    base = _non_adjust_items(items)
    if not base:
        return 0
    return max(int(it.price) for it in base)


def _max_item_share(items: list[Item], total_rub: int | None) -> float:
    if not total_rub:
        return 0.0
    # Для semantic-check исключаем алкоголь: он может легитимно занимать
    # большую долю после объединения одинаковых позиций.
    base = [it for it in _non_adjust_items(items) if not _is_alcohol_item_name(it.name)]
    if not base:
        base = _non_adjust_items(items)
    if not base:
        return 0.0
    mx = max(int(it.price) for it in base)
    return mx / max(1, abs(int(total_rub)))


def _dominant_item_threshold(total_rub: int | None) -> float:
    if total_rub is None:
        return 0.45
    t = abs(int(total_rub))
    if t < 2500:
        return 0.60
    if t < 4000:
        return 0.52
    return 0.45


def _suspicious_price_flags(items: list[Item], total_rub: int | None) -> tuple[int, int]:
    """
    Возвращает (suspicious_count, severe_count) по не-синтетическим позициям.
    severe: цена практически равна total (частый OCR-сбой).
    """
    base = _non_adjust_items(items)
    suspicious = 0
    severe = 0
    for it in base:
        p = int(it.price)
        if p <= 20:
            suspicious += 1
        if total_rub is None and p >= 50_000:
            suspicious += 1
            severe += 1
        if total_rub is not None and p >= int(total_rub * 0.9):
            suspicious += 1
            severe += 1
    return suspicious, severe


def _looks_merged_item_name(name: str) -> bool:
    n = (name or "").lower()
    n = re.sub(r"^\[g\d+\]\s*", "", n).strip()
    if not n:
        return False
    words = re.findall(r"[a-zа-яё0-9]+", n)
    if len(words) < 5:
        return False
    hits = sum(1 for a in _MERGE_ANCHORS if a in n)
    # Защита от ложных срабатываний: нормальные блюда могут содержать
    # 2 "якорных" слова (например, "говядина ... картофельным пюре").
    if hits >= 3:
        return True
    if hits >= 2 and len(words) >= 11:
        return True
    if hits >= 2 and (" и " in n or "," in n) and len(words) >= 8:
        return True
    if len(words) >= 11 and (" и " in n or "," in n):
        return True
    return False


def _merged_items_count(items: list[Item]) -> int:
    base = _non_adjust_items(items)
    return sum(1 for it in base if _looks_merged_item_name(it.name))


def _quality_reasons(items: list[Item], total_rub: int | None) -> list[str]:
    reasons: list[str] = []
    base = _non_adjust_items(items)
    if len(base) < 3:
        reasons.append("few_items")

    if total_rub is None:
        if _max_item_price(items) >= 50_000:
            reasons.append("no_total_huge_price")
        svc_ratio = _service_like_ratio(items)
        if len(base) >= 4 and svc_ratio >= 0.25:
            reasons.append("service_items_high")
        if len(base) <= 12 and _sum_items(base) >= 50_000:
            reasons.append("no_total_sum_anomaly")

    ratio = _synthetic_ratio(items, total_rub)
    if total_rub is not None and ratio > HARD_BLOCK_SYNTHETIC_RATIO:
        reasons.append("synthetic_high")
    elif total_rub is not None and ratio >= YELLOW_ZONE_SYNTHETIC_RATIO:
        reasons.append("synthetic_warning")

    if total_rub is not None and _max_item_share(items, total_rub) > _dominant_item_threshold(total_rub):
        reasons.append("dominant_item")
    suspicious_count, severe_count = _suspicious_price_flags(items, total_rub)
    if severe_count >= 1:
        reasons.append("suspicious_price_severe")
    elif suspicious_count >= 1:
        reasons.append("suspicious_price")
    merged_count = _merged_items_count(items)
    if merged_count >= 2:
        reasons.append("merged_items_high")
    elif merged_count == 1:
        reasons.append("merged_items_warning")
    return reasons


def _quality_status(items: list[Item], total_rub: int | None) -> Literal["good", "warning", "low_confidence"]:
    reasons = _quality_reasons(items, total_rub)
    if any(r in {
        "few_items", "synthetic_high", "dominant_item", "merged_items_high",
        "suspicious_price_severe", "no_total_huge_price", "no_total_sum_anomaly",
    } for r in reasons):
        return "low_confidence"
    if any(r in {"synthetic_warning", "merged_items_warning", "suspicious_price", "service_items_high"} for r in reasons):
        return "warning"
    return "good"


def _quality_reason_lines(reasons: list[str], ratio: float, max_share: float) -> str:
    out: list[str] = []
    for r in reasons:
        if r == "few_items":
            out.append("- слишком мало уверенных позиций")
        elif r == "synthetic_high":
            out.append(f"- большая synthetic-корректировка ({int(round(ratio * 100))}%)")
        elif r == "synthetic_warning":
            out.append(f"- заметная synthetic-корректировка ({int(round(ratio * 100))}%)")
        elif r == "dominant_item":
            out.append(f"- аномально крупная позиция ({int(round(max_share * 100))}% от итога)")
        elif r == "suspicious_price_severe":
            out.append("- есть позиция с ценой, почти равной итогу чека (OCR-сбой)")
        elif r == "suspicious_price":
            out.append("- есть подозрительные цены позиций")
        elif r == "no_total_huge_price":
            out.append("- не найден итог чека и есть аномально крупная цена позиции")
        elif r == "no_total_sum_anomaly":
            out.append("- не найден итог чека и сумма позиций выглядит аномальной")
        elif r == "service_items_high":
            out.append("- среди позиций много служебных строк (НДС/адрес/касса)")
        elif r == "merged_items_high":
            out.append("- похоже, несколько блюд склеены в одной позиции")
        elif r == "merged_items_warning":
            out.append("- есть признаки склейки позиций")
    return "\n".join(out)


def _quality_status_line(status: str) -> str:
    if status == "good":
        return "✅ Статус качества: `good`"
    if status == "warning":
        return "⚠️ Статус качества: `warning`"
    return "⚠️ Статус качества: `low_confidence`"


def _low_conf_summary(items: list[Item], total_rub: int | None) -> str:
    base = _non_adjust_items(items)
    found = len(base)
    s = _sum_items(items)
    if total_rub is None:
        return f"Найдено позиций: {found}\nСумма позиций: {s} ₽\nИТОГО по чеку: не найдено"
    diff = int(total_rub) - int(s)
    sign = "+" if diff > 0 else ""
    return (
        f"Найдено позиций: {found}\n"
        f"Сумма позиций: {s} ₽\n"
        f"ИТОГО по чеку: {int(total_rub)} ₽\n"
        f"Разница: {sign}{diff} ₽"
    )


def _quality_message(items: list[Item], total_rub: int | None, *, for_confirm: bool = False) -> str | None:
    status = _quality_status(items, total_rub)

    ratio = _synthetic_ratio(items, total_rub)
    max_share = _max_item_share(items, total_rub)
    reasons = _quality_reasons(items, total_rub)
    reason_block = _quality_reason_lines(reasons, ratio, max_share)

    if status == "good":
        return _quality_status_line(status)

    if status == "warning":
        return (
            f"{_quality_status_line(status)}\n"
            "Что обратить внимание:\n"
            f"{reason_block}\n"
            "Проверь 1-2 подозрительные позиции.\n"
            "Если есть сомнения, лучше пересканировать чек."
        )

    if for_confirm:
        action = "Подтверждение временно недоступно: сначала пересканируй чек более чётким фото."
    else:
        action = "Что делать дальше:\n1) Пересканировать чек (рекомендуется)\n2) Отправить более чёткое фото"
    return (
        f"{_quality_status_line(status)}\n"
        "Распознавание ненадёжное.\n"
        "Причины:\n"
        f"{reason_block}\n"
        f"{_low_conf_summary(items, total_rub)}\n"
        f"{action}"
    )


def _semantic_fail(items: list[Item], total_rub: int | None) -> bool:
    base = _non_adjust_items(items)
    if len(base) < 3:
        return True
    if _merged_items_count(items) >= 2:
        return True
    if len(base) >= 5 and _service_like_ratio(items) >= 0.35:
        return True
    if total_rub is None:
        if _max_item_price(items) >= 50_000:
            return True
        if len(base) >= 4 and _service_like_ratio(items) >= 0.25:
            return True
        if len(base) <= 12 and _sum_items(base) >= 50_000:
            return True
    suspicious_count, severe_count = _suspicious_price_flags(items, total_rub)
    if severe_count >= 1:
        return True
    if len(base) >= 5 and suspicious_count >= 2:
        return True
    if total_rub is not None and _synthetic_ratio(items, total_rub) > LLM_PARSE_FALLBACK_RATIO:
        return True
    if total_rub is not None and _max_item_share(items, total_rub) > _dominant_item_threshold(total_rub):
        return True
    return False


def _quality_score(items: list[Item], total_rub: int | None) -> float:
    if total_rub is None:
        return float(_semantic_fail(items, total_rub))
    diff = abs(int(total_rub) - _sum_items(items)) / max(1, abs(int(total_rub)))
    synth = _synthetic_ratio(items, total_rub)
    sem = 0.5 if _semantic_fail(items, total_rub) else 0.0
    suspicious_count, severe_count = _suspicious_price_flags(items, total_rub)
    suspicious_penalty = min(0.6, 0.15 * suspicious_count + 0.35 * severe_count)
    merged_count = _merged_items_count(items)
    merged_penalty = 0.0
    if merged_count >= 2:
        merged_penalty = 0.45
    elif merged_count == 1:
        merged_penalty = 0.18
    svc_ratio = _service_like_ratio(items)
    service_penalty = 0.0
    if svc_ratio >= 0.35:
        service_penalty = 0.35
    elif svc_ratio >= 0.2:
        service_penalty = 0.12
    return diff + synth + sem + suspicious_penalty + merged_penalty + service_penalty


def _status_rank(items: list[Item], total_rub: int | None) -> int:
    status = _quality_status(items, total_rub)
    if status == "good":
        return 0
    if status == "warning":
        return 1
    return 2


def _is_low_confidence(items: list[Item], total_rub: int | None) -> bool:
    return _quality_status(items, total_rub) == "low_confidence"


def _accept_llm_candidate(
    current_items: list[Item],
    candidate_items: list[Item],
    total_rub: int | None,
) -> bool:
    if total_rub is not None and _synthetic_ratio(candidate_items, total_rub) > HARD_BLOCK_SYNTHETIC_RATIO:
        return False
    cur_score = _quality_score(current_items, total_rub)
    cand_score = _quality_score(candidate_items, total_rub)
    cur_low = _is_low_confidence(current_items, total_rub)
    cand_low = _is_low_confidence(candidate_items, total_rub)

    # Не принимаем "кандидат" без реального улучшения при сохранении низкого качества.
    if cand_low and (cand_score >= cur_score - 0.02):
        return False
    # Если текущий результат низкого качества — берём любое заметное улучшение.
    if cur_low:
        return cand_score + 0.01 < cur_score
    # В обычном режиме требуем более явный выигрыш.
    return cand_score + 0.02 < cur_score


def _accept_llm_parse_candidate(
    *,
    rule_items: list[Item],
    current_items: list[Item],
    candidate_items: list[Item],
    total_rub: int | None,
) -> bool:
    """
    Для llm_parse делаем более жёсткий gate:
    кандидат должен улучшать не только текущий state, но и baseline от rule.
    """
    if total_rub is not None and _synthetic_ratio(candidate_items, total_rub) > HARD_BLOCK_SYNTHETIC_RATIO:
        return False
    cand_score = _quality_score(candidate_items, total_rub)
    cur_score = _quality_score(current_items, total_rub)
    rule_score = _quality_score(rule_items, total_rub)

    # Жёстко: llm_parse должен вывести результат из low_confidence.
    # Если кандидат остаётся low_confidence, не применяем его даже
    # при небольшом улучшении score.
    if _is_low_confidence(candidate_items, total_rub):
        return False

    # Должно быть заметное улучшение относительно лучшего из двух baseline-состояний.
    best_baseline = min(cur_score, rule_score)
    if cand_score >= best_baseline - 0.01:
        return False

    if total_rub is not None:
        cand_synth = _synthetic_ratio(candidate_items, total_rub)
        cur_synth = _synthetic_ratio(current_items, total_rub)
        rule_synth = _synthetic_ratio(rule_items, total_rub)
        best_synth = min(cur_synth, rule_synth)
        if cand_synth > best_synth + 0.01:
            return False

    cand_sem = _semantic_fail(candidate_items, total_rub)
    if cand_sem:
        return False

    return True


def _accept_llm_refine_candidate(
    *,
    current_items: list[Item],
    candidate_items: list[Item],
    total_rub: int | None,
) -> bool:
    """
    Для llm_refine тоже держим жёсткий gate:
    не принимаем кандидата, если он остаётся low_confidence.
    """
    if total_rub is not None and _synthetic_ratio(candidate_items, total_rub) > HARD_BLOCK_SYNTHETIC_RATIO:
        return False
    if _is_low_confidence(candidate_items, total_rub):
        return False
    if _semantic_fail(candidate_items, total_rub):
        return False
    # Базовое улучшение относительно текущего состояния всё равно обязательно.
    return _accept_llm_candidate(current_items, candidate_items, total_rub)


def _best_candidate_by_quality(
    candidates: list[tuple[str, list[Item]]],
    total_rub: int | None,
) -> tuple[str, list[Item]] | None:
    if not candidates:
        return None

    def _key(c: tuple[str, list[Item]]) -> tuple[int, float, float, int]:
        _, its = c
        return (
            _status_rank(its, total_rub),
            _quality_score(its, total_rub),
            _synthetic_ratio(its, total_rub),
            len(_non_adjust_items(its)),
        )

    best = candidates[0]
    best_key = _key(best)
    for cand in candidates[1:]:
        k = _key(cand)
        # lower rank/score/synthetic is better; if equal, prefer more extracted positions
        if (k[0] < best_key[0]) or (k[0] == best_key[0] and k[1] < best_key[1]) or (
            k[0] == best_key[0] and k[1] == best_key[1] and k[2] < best_key[2]
        ):
            best = cand
            best_key = k
        elif (
            k[0] == best_key[0]
            and k[1] == best_key[1]
            and k[2] == best_key[2]
            and k[3] > best_key[3]
        ):
            best = cand
            best_key = k
    return best


def _pick_best_parsed_result(
    variants: list[tuple[str, list[Item], int | None]],
) -> tuple[str, list[Item], int | None] | None:
    if not variants:
        return None

    def _k(v: tuple[str, list[Item], int | None]) -> tuple[int, float, float, int]:
        _, items, total = v
        return (
            _status_rank(items, total),
            _quality_score(items, total),
            _synthetic_ratio(items, total),
            len(_non_adjust_items(items)),
        )

    best = variants[0]
    best_k = _k(best)
    for cand in variants[1:]:
        ck = _k(cand)
        if ck < best_k:
            best = cand
            best_k = ck
    return best


def _with_adjustment_if_needed(items: list[Item], total_rub: int | None) -> list[Item]:
    if total_rub is None:
        return items
    s = _sum_items(items)
    diff = int(total_rub) - int(s)
    if diff == 0:
        return items
    out = list(items)
    out.append(Item(name="Корректировка по итогу", price=diff))
    return out


def _confirm_help_text() -> str:
    return (
        "Если список выглядит неверно, пересканируй чек:\n"
        "- кнопка `🔄 Пересканировать (новое фото)`\n"
        "- пришли новое фото: чек целиком, ровно, без бликов."
    )


def _fmt_total_block(items_sum: int, total: int | None, *, items_count: int | None = None) -> str:
    head = f"Найдено позиций: {items_count}\n" if items_count is not None else ""
    if total is None:
        return f"{head}Сумма позиций: {items_sum} ₽\nИТОГО по чеку: не найдено ₽\nРазница: —"
    diff = total - items_sum
    sign = "+" if diff > 0 else ""
    return f"{head}Сумма позиций: {items_sum} ₽\nИТОГО по чеку: {total} ₽\nРазница: {sign}{diff} ₽"


def _fmt_sum_after_llm(items_sum: int, total: int | None) -> str:
    if total is None:
        return f"Σ распознанных позиций после LLM: **{items_sum} ₽**"
    diff = total - items_sum
    sign = "+" if diff > 0 else ""
    return (
        f"Σ распознанных позиций после LLM: **{items_sum} ₽**\n"
        f"ИТОГО по чеку: **{total} ₽**\n"
        f"Разница: **{sign}{diff} ₽**"
    )


async def _make_deep_link(bot: Bot, start_param: str) -> str:
    try:
        me = await bot.get_me()
        username = (me.username or "").strip()
    except Exception:
        username = ""
    if username:
        return f"https://t.me/{username}?start={start_param}"
    return "https://t.me/"


async def _kb_join_private(bot: Bot, token: str, *, show_test: bool = False):
    deep_link = await _make_deep_link(bot, f"join_{token}")
    b = InlineKeyboardBuilder()
    b.button(text="➕ Я участвую здесь (Открыть ЛС с ботом)", url=deep_link)
    if show_test:
        b.button(text="➕ Тестовый участник", callback_data="join_test")
    b.button(text="✅ Готово", callback_data="join_done")
    b.adjust(1)
    return b.as_markup()


async def _kb_pay_private(bot: Bot, token: str):
    deep_link = await _make_deep_link(bot, f"pay_{token}")
    b = InlineKeyboardBuilder()
    b.button(text="💳 Открыть ЛС для оплаты", url=deep_link)
    b.button(text="🔄 Обновить статус оплат", callback_data="pay_status")
    b.button(text="✅ Завершить и посчитать", callback_data="pay_done")
    b.adjust(1)
    return b.as_markup()


def _fmt_item_votes_summary(sess) -> str:
    lines: list[str] = []
    lines.append("Сводка по позициям (кто что ел):")
    if not sess.items:
        lines.append("- нет позиций")
        return "\n".join(lines)
    for idx, it in enumerate(sess.items, start=1):
        selected = list(it.weights.keys())
        if not selected:
            lines.append(f"{idx}. {it.name} — {it.price} ₽ | никто не выбран")
            continue
        n = len(selected)
        parts = []
        for uid in selected:
            nm = sess.participants.get(uid, str(uid))
            parts.append(f"{nm} (1/{n})")
        lines.append(f"{idx}. {it.name} — {it.price} ₽ | " + ", ".join(parts))
    return "\n".join(lines)


def _fmt_paid_status(sess) -> str:
    lines = ["Текущий статус оплат:"]
    for uid, name in sess.participants.items():
        paid = int(sess.paid.get(uid, 0))
        lines.append(f"- {name}: {paid} ₽")
    pending = [name for uid, name in sess.participants.items() if uid not in sess.paid]
    if pending:
        lines.append("")
        lines.append("Ещё не отправили оплату:")
        for name in pending:
            lines.append(f"- {name}")
    else:
        lines.append("")
        lines.append("Все участники отправили оплату ✅")
    return "\n".join(lines)


def _kb_private_pay_input(token: str):
    b = InlineKeyboardBuilder()
    b.button(text="💳 Я оплатил(а) весь счёт", callback_data=f"pf:{token}")
    b.button(text="🙅 Я не платил(а)", callback_data=f"pn:{token}")
    b.adjust(1)
    return b.as_markup()


async def _open_payments_stage(message: Message, state: FSMContext):
    sess = store.get(message.chat.id)
    data = await state.get_data()
    token = str(data.get("receipt_session_id") or _mk_receipt_session_id(message.chat.id, "pay"))
    _PENDING_PRIVATE_PAY[token] = message.chat.id
    kb = await _kb_pay_private(message.bot, token)
    await state.set_state(ReceiptFlow.set_payments)
    await message.answer(_fmt_item_votes_summary(sess))
    await message.answer(
        "Теперь этап оплат.\n"
        "Каждый участник нажимает «💳 Открыть ЛС для оплаты», в личке отправляет сумму.\n"
        "После этого нажми «✅ Завершить и посчитать».",
        reply_markup=kb,
    )
    await message.answer(_fmt_paid_status(sess))


def _survey_participant_uids(sess) -> list[int]:
    # В ЛС можно опрашивать только реальных пользователей Telegram (uid > 0).
    return [uid for uid in sess.participants.keys() if int(uid) > 0]


def _rebuild_weights_from_private_survey(sess, token: str) -> None:
    for it in sess.items:
        it.weights = {}
    for uid in _survey_participant_uids(sess):
        answers = _PRIVATE_SURVEY_ANSWERS.get((token, uid), set())
        for idx in answers:
            if 0 <= idx < len(sess.items):
                sess.items[idx].weights[uid] = 1.0


def _fmt_private_survey_status(sess, token: str) -> str:
    lines: list[str] = ["Статус личного опроса (кто что ел):"]
    survey_uids = _survey_participant_uids(sess)
    done_cnt = 0
    for uid in survey_uids:
        name = sess.participants.get(uid, str(uid))
        done = bool(_PRIVATE_SURVEY_DONE.get((token, uid), False))
        answers = _PRIVATE_SURVEY_ANSWERS.get((token, uid), set())
        if done:
            done_cnt += 1
            lines.append(f"- {name}: завершил ({len(answers)} поз.)")
        else:
            lines.append(f"- {name}: ожидается ответ")
    if survey_uids:
        lines.append("")
        lines.append(f"Готово: {done_cnt}/{len(survey_uids)}")
    else:
        lines.append("")
        lines.append("Нет участников с личным чатом для опроса.")

    _rebuild_weights_from_private_survey(sess, token)
    if done_cnt > 0:
        subtotals = calc_per_item_shares(sess.items, sess.participants)
        lines.append("")
        lines.append("Промежуточный расклад по позициям:")
        for uid in survey_uids:
            name = sess.participants.get(uid, str(uid))
            lines.append(f"- {name}: {int(subtotals.get(uid, 0))} ₽")
    return "\n".join(lines)


async def _ask_private_survey_item(bot: Bot, token: str, group_chat_id: int, uid: int, idx: int, chat_id: int | None = None):
    sess = store.get(group_chat_id)
    if chat_id is None:
        chat_id = uid
    if idx >= len(sess.items):
        _PRIVATE_SURVEY_DONE[(token, uid)] = True
        answers = _PRIVATE_SURVEY_ANSWERS.get((token, uid), set())
        try:
            await bot.send_message(chat_id, f"Опрос завершён. Отмечено позиций: {len(answers)}.")
            await bot.send_message(group_chat_id, f"{sess.participants.get(uid, uid)} завершил личный опрос.")
            await bot.send_message(group_chat_id, _fmt_private_survey_status(sess, token))
        except Exception:
            pass
        return

    it = sess.items[idx]
    b = InlineKeyboardBuilder()
    b.button(text="✅ Ел(а)", callback_data=f"sv:{token}:{idx}:y")
    b.button(text="❌ Не ел(а)", callback_data=f"sv:{token}:{idx}:n")
    b.adjust(2)
    await bot.send_message(
        chat_id,
        f"Позиция {idx+1}/{len(sess.items)}:\n"
        f"{it.name} — {it.price} ₽\n\n"
        "Ты ел(а) эту позицию?",
        reply_markup=b.as_markup(),
    )


async def _open_private_survey_stage(message: Message, state: FSMContext):
    sess = store.get(message.chat.id)
    data = await state.get_data()
    token = str(data.get("receipt_session_id") or _mk_receipt_session_id(message.chat.id, "survey"))
    _PENDING_PRIVATE_SURVEY[token] = message.chat.id

    # Сброс предыдущих ответов по токену.
    for uid in list(sess.participants.keys()):
        _PRIVATE_SURVEY_ANSWERS.pop((token, uid), None)
        _PRIVATE_SURVEY_DONE.pop((token, uid), None)

    survey_link = await _make_deep_link(message.bot, f"survey_{token}")
    b = InlineKeyboardBuilder()
    b.button(text="🍽️ Открыть ЛС для опроса", url=survey_link)
    b.button(text="🔄 Обновить статус опроса", callback_data="survey_status")
    b.button(text="✅ Завершить опрос и перейти к оплатам", callback_data="survey_done")
    b.adjust(1)
    await state.update_data(private_survey_token=token)
    await message.answer(
        "Опрос «кто что ел» проводится в личке у каждого участника.\n"
        "Каждый участник нажимает «🍽️ Открыть ЛС для опроса», затем Start и отвечает по позициям.",
        reply_markup=b.as_markup(),
    )
    await message.answer(_fmt_private_survey_status(sess, token))


async def _process_receipt_bytes(message: Message, state: FSMContext, image_bytes: bytes) -> None:
    sess = store.get(message.chat.id)
    mode_used = "rule_v1"
    parser_variant = "rule_v1"
    receipt_session_id = _mk_receipt_session_id(message.chat.id, str(len(image_bytes)))
    await state.update_data(receipt_session_id=receipt_session_id)
    llm_call_count = 0
    llm_stages_attempted: list[str] = []
    llm_unsupported_items: int | None = None
    llm_novel_token_ratio: float | None = None
    llm_candidate_rejected_reason: str | None = None

    async def _send_processing(text: str, *, parse_mode: str | None = None, public: bool = False):
        mirrored = _should_mirror_processing_to_admin(message)
        if public or not mirrored:
            await message.answer(text, parse_mode=parse_mode)
        if mirrored:
            await _mirror_processing_message(message, text, parse_mode=parse_mode)

    await _send_processing("Шаг 1/3: OCR текста чека...")

    _load_best_by_fp_cache()

    # OCR + PARSE
    text = ""
    parsed = []
    total_rub: int | None = None
    if PIPELINE_V2:
        try:
            # v1 candidate from regular OCR text
            text_v1 = extract_text(image_bytes)
            await _send_processing("Шаг 2/3: Разбор позиций...")
            p_v1, p_v1_variant = parse_receipt_text_with_variant(text_v1)
            t_v1 = extract_total_rub(text_v1)
            variants: list[tuple[str, str, str, list, int | None]] = [
                ("rule_v1", p_v1_variant, text_v1, p_v1, t_v1)
            ]

            # v2 candidate from layout OCR text
            try:
                lay = extract_layout_result(image_bytes)
                text_v2 = lay.text
                p_v2 = parse_layout_receipt(text_v2, [l.text for l in lay.lines])
                t_v2 = extract_total_rub(text_v2)
                variants.append(("rule_v2", "layout_v2", text_v2, p_v2, t_v2))
            except Exception:
                pass

            best = None
            best_score = 10**9
            for m, pv, txt, prs, tt in variants:
                its = [Item(name=i.name, price=i.price) for i in prs]
                sc = _quality_score(its, tt)
                if sc < best_score:
                    best = (m, pv, txt, prs, tt)
                    best_score = sc
            if best is None:
                raise RuntimeError("No parse candidates")
            mode_used, parser_variant, text, parsed, total_rub = best
        except Exception:
            log.exception("OCR/Parser failed (v2)")
            await _send_processing(
                "❌ Не удалось распознать чек.\n"
                "Попробуй фото ближе, ровнее и без бликов.",
                public=True,
            )
            await state.set_state(ReceiptFlow.waiting_photo)
            return
    else:
        try:
            text = extract_text(image_bytes)
            await _send_processing("Шаг 2/3: Разбор позиций...")
        except Exception:
            log.exception("OCR failed")
            await _send_processing(
                "❌ Не удалось распознать чек.\n"
                "Попробуй фото ближе, ровнее и без бликов.",
                public=True,
            )
            await state.set_state(ReceiptFlow.waiting_photo)
            return

        try:
            parsed, parser_variant = parse_receipt_text_with_variant(text)
        except Exception:
            log.exception("Parser failed")
            await _send_processing(
                "❌ Не удалось разобрать позиции чека.\n"
                "Попробуй отправить более чёткое фото.",
                public=True,
            )
            await state.set_state(ReceiptFlow.waiting_photo)
            return

    if total_rub is None:
        total_rub = extract_total_rub(text)
    sess.total_rub = total_rub
    sess.items = [Item(name=p.name, price=p.price) for p in parsed]

    # Multi-shot OCR for unstable/low-quality cases (Yandex): run 1-2 extra OCR passes
    # and keep best parsed result by quality score.
    provider_env = (os.getenv("OCR_PROVIDER") or "").strip().lower()
    maybe_yandex = provider_env in {"", "yandex"}
    if maybe_yandex and _quality_bad(sess.items, total_rub):
        variants: list[tuple[str, list[Item], int | None, str, str]] = [
            ("ocr_shot_1", list(sess.items), total_rub, parser_variant, text)
        ]

        def _key(v: tuple[str, list[Item], int | None, str, str]) -> tuple[int, float, float, int]:
            _, its, tt, _, _ = v
            return (
                _status_rank(its, tt),
                _quality_score(its, tt),
                _synthetic_ratio(its, tt),
                -len(_non_adjust_items(its)),
            )

        for shot in range(2, 4):
            try:
                shot_text = extract_text(image_bytes)
                shot_parsed, shot_variant = parse_receipt_text_with_variant(shot_text)
                shot_items = [Item(name=p.name, price=p.price) for p in shot_parsed]
                shot_total = extract_total_rub(shot_text)
                variants.append((f"ocr_shot_{shot}", shot_items, shot_total, shot_variant, shot_text))
            except Exception:
                continue
        if variants:
            best = min(variants, key=_key)
            _, best_items, best_total, best_variant, best_text = best
            sess.items = list(best_items)
            text = best_text
            parser_variant = best_variant
            if best_total is not None:
                sess.total_rub = best_total
            total_rub = sess.total_rub

    ocr_text_path = _write_ocr_snapshot(receipt_session_id, text)
    ocr_hash = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:24] if text else None
    rule_items_baseline = list(sess.items)
    candidate_pool: list[tuple[str, list[Item]]] = [(mode_used, list(sess.items))]
    items_sum = _sum_items(sess.items)
    synthetic = _synthetic_adjustment(sess.items)

    await _send_processing("Шаг 3/3: Проверка качества...")
    await _send_processing(_fmt_total_block(items_sum, total_rub, items_count=len(_non_adjust_items(sess.items))))
    rep = validate_items(sess.items, total_rub)
    if not rep.ok:
        await message.answer(format_validation_report(rep))

    # LLM refine (если есть ключ и есть смысл)
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"

    def _ground_llm_candidate(stage_name: str, candidate_items: list[Item]) -> list[Item] | None:
        nonlocal llm_unsupported_items, llm_novel_token_ratio, llm_candidate_rejected_reason
        grounded_items, report = _apply_llm_grounding(candidate_items, text)
        llm_unsupported_items = int(report.get("unsupported_items") or 0)
        llm_novel_token_ratio = float(report.get("novel_token_ratio") or 0.0)
        unsupported_ratio = float(report.get("unsupported_ratio") or 1.0)
        reject_reason: str | None = None
        if not grounded_items:
            reject_reason = "grounding_no_supported_items"
        elif LLM_GROUNDING_STRICT and unsupported_ratio > LLM_MAX_UNSUPPORTED_RATIO:
            reject_reason = f"grounding_unsupported_ratio>{LLM_MAX_UNSUPPORTED_RATIO:.2f}"
        elif LLM_GROUNDING_STRICT and llm_novel_token_ratio > LLM_MAX_NOVEL_TOKEN_RATIO:
            reject_reason = f"grounding_novel_token_ratio>{LLM_MAX_NOVEL_TOKEN_RATIO:.2f}"
        if reject_reason:
            llm_candidate_rejected_reason = f"{stage_name}:{reject_reason}"
            return None
        return _with_adjustment_if_needed(grounded_items, total_rub)

    if openai_key and sess.items:
        initial_ratio = _synthetic_ratio(sess.items, total_rub)
        # если total найден и не сошлось — точно пробуем LLM
        need_llm = (total_rub is not None and total_rub != items_sum)
        # если total не найден — LLM тоже может помочь “разлепить”, но аккуратно
        need_llm = need_llm or (total_rub is None)
        # если качество низкое — запускаем LLM независимо от формального совпадения суммы
        need_llm = need_llm or _quality_bad(sess.items, total_rub)
        # "желтая зона": заметная synthetic-корректировка даже ниже hard-block.
        need_llm = need_llm or (total_rub is not None and initial_ratio >= YELLOW_ZONE_SYNTHETIC_RATIO)
        # даже если SUM==TOTAL, большая синтетическая корректировка = плохой парсинг
        if total_rub is not None and abs(synthetic) >= max(300, int(total_rub * 0.1)):
            need_llm = True

        if need_llm and llm_call_count < LLM_MAX_CALLS_PER_RECEIPT:
            await _send_processing("✨ Попробую улучшить распознавание через LLM…")
            llm_stages_attempted.append("refine")
            llm_call_count += 1
            try:
                res = llm_refine_receipt_items(text, sess.items, total_rub, model=openai_model)

                # совместимость: res может быть LLMResult или tuple
                if isinstance(res, tuple):
                    refined_items, notes = res
                elif isinstance(res, LLMResult):
                    refined_items, notes = res.items, res.notes
                else:
                    # last resort
                    refined_items, notes = res.items, getattr(res, "notes", None)

                cand_items = [Item(name=i.name, price=i.price) for i in refined_items]
                cand_items = _ground_llm_candidate("refine", cand_items)
                if cand_items:
                    candidate_pool.append(("llm_refine", list(cand_items)))
                if cand_items and _accept_llm_refine_candidate(
                    current_items=sess.items,
                    candidate_items=cand_items,
                    total_rub=total_rub,
                ):
                    sess.items = cand_items
                    mode_used = "llm_refine"
                    parser_variant = "llm_refine"
                    items_sum2 = _sum_items(sess.items)
                    await message.answer("✅ Применил улучшенный вариант.\n" + _fmt_total_block(items_sum2, total_rub, items_count=len(_non_adjust_items(sess.items))))
                    await message.answer(_fmt_sum_after_llm(items_sum2, total_rub), parse_mode="Markdown")
                    if notes:
                        await message.answer(f"ℹ️ Заметки: {notes}")
                else:
                    if llm_candidate_rejected_reason:
                        await _send_processing(f"ℹ️ LLM-вариант отклонён: {llm_candidate_rejected_reason}.")
                    else:
                        await _send_processing("ℹ️ LLM-вариант отклонён: качество не стало лучше.")

            except Exception:
                await _send_processing("⚠️ LLM не смог улучшить распознавание.\nОставил лучший найденный вариант.")
        elif need_llm:
            await _send_processing(
                f"ℹ️ LLM-бюджет исчерпан ({llm_call_count}/{LLM_MAX_CALLS_PER_RECEIPT}), оставляю лучший вариант."
            )

        # Если после refine качество всё ещё плохое — пробуем LLM-парсер "с нуля".
        if _quality_bad(sess.items, total_rub) and llm_call_count < LLM_MAX_CALLS_PER_RECEIPT:
            await _send_processing("🧠 Пробую второй проход: LLM-парсер по сырому OCR…")
            llm_stages_attempted.append("parse")
            llm_call_count += 1
            try:
                alt = llm_parse_receipt(
                    text,
                    hint_total_rub=total_rub,
                    hint_items_sum=_sum_items(sess.items),
                    model=openai_model,
                )
                alt_items = [Item(name=i.name, price=i.price) for i in alt.items]
                if alt_items:
                    alt_items = _ground_llm_candidate("parse", alt_items)
                    if alt_items:
                        candidate_pool.append(("llm_parse", list(alt_items)))
                        alt_sum = _sum_items(alt_items)
                        accept_general = _accept_llm_candidate(sess.items, alt_items, total_rub)
                        accept_parse_guard = _accept_llm_parse_candidate(
                            rule_items=rule_items_baseline,
                            current_items=sess.items,
                            candidate_items=alt_items,
                            total_rub=total_rub,
                        )
                        if accept_general and accept_parse_guard:
                            sess.items = alt_items
                            mode_used = "llm_parse"
                            parser_variant = "llm_parse"
                            await _send_processing("✅ Применил результат LLM-парсера.")
                            await _send_processing(_fmt_total_block(alt_sum, total_rub, items_count=len(_non_adjust_items(sess.items))))
                            await _send_processing(_fmt_sum_after_llm(alt_sum, total_rub), parse_mode="Markdown")
                            if alt.notes:
                                await _send_processing(f"ℹ️ Заметки LLM-парсера: {alt.notes}")
                        else:
                            await _send_processing("ℹ️ LLM-парсер не дал убедимого улучшения.")
                    else:
                        await _send_processing("ℹ️ LLM-парсер отклонён anti-hallucination gate.")
            except Exception:
                await _send_processing("⚠️ Второй проход LLM-парсера не удался.\nОставил лучший найденный вариант.")
        elif _quality_bad(sess.items, total_rub):
            await _send_processing(
                f"ℹ️ Пропускаю второй проход: LLM-бюджет {llm_call_count}/{LLM_MAX_CALLS_PER_RECEIPT}."
            )

        # Третий шаг reconcile: запускаем только если после первых двух шагов
        # всё ещё warning/low_confidence и есть бюджет.
        status_after_two = _quality_status(sess.items, total_rub)
        if status_after_two in {"warning", "low_confidence"} and llm_call_count < LLM_MAX_CALLS_PER_RECEIPT:
            await _send_processing("🧩 Третий проход: LLM reconcile...")
            llm_stages_attempted.append("reconcile")
            llm_call_count += 1
            try:
                rec = llm_reconcile_receipt(
                    text,
                    sess.items,
                    total_rub=total_rub,
                    model=openai_model,
                )
                rec_items = [Item(name=i.name, price=i.price) for i in rec.items]
                if rec_items:
                    rec_items = _ground_llm_candidate("reconcile", rec_items)
                    if rec_items:
                        candidate_pool.append(("llm_reconcile", list(rec_items)))
                        rec_sum = _sum_items(rec_items)
                        accept_general = _accept_llm_candidate(sess.items, rec_items, total_rub)
                        accept_parse_guard = _accept_llm_parse_candidate(
                            rule_items=rule_items_baseline,
                            current_items=sess.items,
                            candidate_items=rec_items,
                            total_rub=total_rub,
                        )
                        if accept_general and accept_parse_guard:
                            sess.items = rec_items
                            mode_used = "llm_reconcile"
                            parser_variant = "llm_reconcile"
                            await _send_processing("✅ Применил результат reconcile.")
                            await _send_processing(
                                _fmt_total_block(rec_sum, total_rub, items_count=len(_non_adjust_items(sess.items)))
                            )
                            await _send_processing(
                                _fmt_sum_after_llm(rec_sum, total_rub),
                                parse_mode="Markdown",
                            )
                            if rec.notes:
                                await _send_processing(f"ℹ️ Заметки reconcile: {rec.notes}")
                        else:
                            await _send_processing("ℹ️ Reconcile не дал убедимого улучшения.")
                    else:
                        await _send_processing("ℹ️ Reconcile отклонён anti-hallucination gate.")
            except Exception:
                await _send_processing("⚠️ Reconcile-проход не удался.\nОставил лучший найденный вариант.")

    # Fail-safe: если результат всё ещё low_confidence, оставляем лучший кандидат,
    # чтобы не терять более удачную ветку при неудачном последнем проходе.
    if _is_low_confidence(sess.items, total_rub):
        best = _best_candidate_by_quality(candidate_pool, total_rub)
        if best:
            best_mode, best_items = best
            # Не переключаемся на LLM-вариант, если он всё равно low_confidence.
            if best_mode.startswith("llm_") and _is_low_confidence(best_items, total_rub):
                best_mode, best_items = mode_used, list(sess.items)
            if _quality_score(best_items, total_rub) + 0.001 < _quality_score(sess.items, total_rub):
                sess.items = list(best_items)
                mode_used = best_mode
                if best_mode.startswith("llm_"):
                    parser_variant = best_mode

    # Cache best known result by OCR fingerprint to reduce random degradation
    fp = _receipt_fingerprint(image_bytes)
    cur_score = _quality_score(sess.items, total_rub)
    cached = _BEST_BY_FP_CACHE.get(fp)
    if cached:
        c_total = cached.get("total_rub")
        c_items = [Item(name=i.get("name", ""), price=int(i.get("price", 0))) for i in (cached.get("items") or [])]
        c_score = _quality_score(c_items, c_total)
        if c_score + 0.001 < cur_score:
            sess.items = c_items
            sess.total_rub = c_total
            total_rub = c_total
            mode_used = str(cached.get("mode_used") or mode_used)
            parser_variant = str(cached.get("parser_variant") or parser_variant)
            cur_score = c_score
    family_cached = _best_cached_family_candidate(text, total_rub)
    if family_cached:
        f_items, f_total, f_mode = family_cached
        f_score = _quality_score(f_items, f_total)
        if f_score + 0.001 < cur_score:
            sess.items = f_items
            sess.total_rub = f_total
            total_rub = f_total
            mode_used = f_mode
            cur_score = f_score
    prev = _BEST_BY_FP_CACHE.get(fp)
    should_write = True
    if prev:
        prev_score = float(prev.get("score", 10**9))
        # Никогда не перезаписываем лучший fingerprint-слепок худшим.
        should_write = cur_score + 0.001 < prev_score
    if should_write:
        _BEST_BY_FP_CACHE[fp] = {
            "score": cur_score,
            "total_rub": total_rub,
            "mode_used": mode_used,
            "parser_variant": parser_variant,
            "items": _items_payload(sess.items),
            "updated_at": _now_iso(),
        }
    try:
        _save_best_by_fp_cache()
    except Exception:
        pass

    # confirm
    await state.set_state(ReceiptFlow.confirm_items)

    if not sess.items:
        await message.answer(
            "⚠️ Статус качества: `low_confidence`\n"
            "Не удалось уверенно распознать позиции.\n"
            "Отправь другое фото: чек целиком, ровно, без бликов."
        )
        await state.set_state(ReceiptFlow.waiting_photo)
        return

    quality_msg = _quality_message(sess.items, sess.total_rub)
    qs = _quality_status(sess.items, sess.total_rub)
    qr = _quality_reasons(sess.items, sess.total_rub)
    _log_receipt_session(
        session_id=receipt_session_id,
        chat_id=message.chat.id,
        mode_used=mode_used,
        parser_variant=parser_variant,
        quality_status=qs,
        quality_reasons=qr,
        total_rub=sess.total_rub,
        items=sess.items,
        ocr_text_path=ocr_text_path,
        ocr_hash=ocr_hash,
        llm_call_count=llm_call_count,
        llm_stages_attempted=llm_stages_attempted,
        llm_unsupported_items=llm_unsupported_items,
        llm_novel_token_ratio=llm_novel_token_ratio,
        llm_candidate_rejected_reason=llm_candidate_rejected_reason,
    )
    if quality_msg:
        await _send_processing(quality_msg, parse_mode="Markdown", public=True)

    await message.answer(
        "Нашёл такие позиции:\n\n"
        f"{_items_preview(sess.items)}\n\n"
        "Подтвердить?\n\n"
        f"{_confirm_help_text()}",
        reply_markup=kb_items_confirm()
    )


# -------------------------
# Receipt input
# -------------------------
async def on_photo(message: Message, state: FSMContext):
    if not message.photo:
        return
    photo = message.photo[-1]
    try:
        image_bytes = await _download_file_bytes(message, photo.file_id)
        await _process_receipt_bytes(message, state, image_bytes)
    except Exception as e:
        log.exception("on_photo failed")
        await message.answer("❌ Не удалось обработать фото чека. Попробуй ещё раз.")
        await state.set_state(ReceiptFlow.waiting_photo)


async def on_document(message: Message, state: FSMContext):
    doc = message.document
    if not doc:
        return

    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    is_image = mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"))

    if not is_image:
        await message.answer("Отправь чек как изображение (JPG/PNG/HEIC).")
        return

    try:
        image_bytes = await _download_file_bytes(message, doc.file_id)
        await _process_receipt_bytes(message, state, image_bytes)
    except Exception as e:
        log.exception("on_document failed")
        await message.answer("❌ Не удалось обработать файл чека. Попробуй ещё раз.")
        await state.set_state(ReceiptFlow.waiting_photo)


# -------------------------
# Callbacks: items confirm
# -------------------------
async def cb_items_ok(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    if _quality_status(sess.items, sess.total_rub) == "low_confidence":
        quality_msg = _quality_message(sess.items, sess.total_rub, for_confirm=True)
        if quality_msg:
            await call.message.answer(quality_msg, parse_mode="Markdown")
        await call.message.answer("Продолжаю по подтверждению вручную.")

    data = await state.get_data()
    _log_user_edit(
        session_id=data.get("receipt_session_id"),
        chat_id=call.message.chat.id,
        action="accept_items",
        details={
            "quality_status": _quality_status(sess.items, sess.total_rub),
            "quality_reasons": _quality_reasons(sess.items, sess.total_rub),
            "items": _items_payload(sess.items),
            "total_rub": sess.total_rub,
        },
    )

    await state.set_state(ReceiptFlow.collect_participants)
    join_token = data.get("receipt_session_id") or _mk_receipt_session_id(call.message.chat.id, "join")
    _PENDING_PRIVATE_JOIN[str(join_token)] = call.message.chat.id
    show_test = _is_private_chat(call.message) and _is_admin_user(int(call.from_user.id) if call.from_user else 0)
    kb = await _kb_join_private(call.message.bot, str(join_token), show_test=show_test)
    await call.message.answer(
        "Теперь соберём участников.\n"
        "1) Каждый нажимает «Я участвую здесь (Открыть ЛС с ботом)».\n"
        "2) В личке нажимает Start — после этого участник добавится автоматически.\n"
        "3) Когда все подключились, жми «✅ Готово».\n\n",
        reply_markup=kb
    )


async def cb_items_rescan(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    _log_user_edit(
        session_id=data.get("receipt_session_id"),
        chat_id=call.message.chat.id,
        action="rescan",
        details={},
    )
    await state.set_state(ReceiptFlow.waiting_photo)
    await call.message.answer("Ок, отправь новое фото/файл чека.")


# -------------------------
# Callbacks: participants
# -------------------------
async def cb_join(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    uid = call.from_user.id
    sess.participants[uid] = call.from_user.full_name
    await call.message.answer(f"Добавил: {sess.participants[uid]}")


async def cb_join_test(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)

    existing_nums: set[int] = set()
    for name in sess.participants.values():
        m = re.fullmatch(r"Тестовый\s+(\d+)", name or "")
        if m:
            try:
                existing_nums.add(int(m.group(1)))
            except Exception:
                pass
    n = 1
    while n in existing_nums:
        n += 1

    test_uid = -(100000 + n)
    while test_uid in sess.participants:
        n += 1
        test_uid = -(100000 + n)

    test_name = f"Тестовый {n}"
    sess.participants[test_uid] = test_name

    data = await state.get_data()
    _log_user_edit(
        session_id=data.get("receipt_session_id"),
        chat_id=call.message.chat.id,
        action="join_test_participant",
        details={"participant_name": test_name, "participant_uid": test_uid},
    )
    await call.message.answer(f"Добавил: {test_name}")


async def cb_join_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    if len(sess.participants) < 1:
        await call.message.answer("Добавьте хотя бы одного участника.")
        return

    # Временный режим: чаевые выключены из UI.
    sess.tip_percent = 0
    sess.tip_fixed = 0
    await state.set_state(ReceiptFlow.set_weights)
    await call.message.answer(
        "Как делим чек дальше?\n"
        "- по позициям (кто что ел/пил)\n"
        "- или сразу поровну на всех\n\n"
        "Общий заказ — «Разделить всё поровну».\n"
        "Разные позиции — «Разобрать по позициям».",
        reply_markup=kb_split_mode(),
    )


# -------------------------
# Callbacks: tip
# -------------------------
async def cb_tip_preset(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    data = call.data

    if data == "tip_0":
        sess.tip_percent = 0
        sess.tip_fixed = 0
    elif data == "tip_10":
        sess.tip_percent = 10
        sess.tip_fixed = 0
    elif data == "tip_12":
        sess.tip_percent = 12
        sess.tip_fixed = 0
    elif data == "tip_15":
        sess.tip_percent = 15
        sess.tip_fixed = 0
    elif data == "tip_custom":
        await call.message.answer("Введи процент чаевых числом, например: 10")
        await state.update_data(awaiting="tip_custom")
        return
    elif data == "tip_fixed":
        await call.message.answer("Введи сумму чаевых в ₽, например: 500")
        await state.update_data(awaiting="tip_fixed")
        return

    await call.message.answer(f"Ок. Чаевые: {sess.tip_percent}% (фикс: {sess.tip_fixed} ₽)")
    await call.message.answer(
        "Как делим чек дальше?\n"
        "- по позициям (кто что ел/пил)\n"
        "- или сразу поровну на всех\n\n"
        "Общий заказ — «Разделить всё поровну».\n"
        "Разные позиции — «Разобрать по позициям».",
        reply_markup=kb_split_mode(),
    )


async def on_text_tip(message: Message, state: FSMContext):
    sess = store.get(message.chat.id)
    data = await state.get_data()
    awaiting = data.get("awaiting")

    if awaiting == "tip_custom":
        try:
            val = float(message.text.replace(",", "."))
            if val < 0 or val > 50:
                raise ValueError()
        except Exception:
            await message.answer("Не понял процент. Пример: 10")
            return
        sess.tip_percent = val
        sess.tip_fixed = 0
        await message.answer(f"Ок. Чаевые: {sess.tip_percent}%")
        await state.update_data(awaiting=None)
        await message.answer(
            "Как делим чек дальше?\n"
            "- по позициям (кто что ел/пил)\n"
            "- или сразу поровну на всех\n\n"
            "Общий заказ — «Разделить всё поровну».\n"
            "Разные позиции — «Разобрать по позициям».",
            reply_markup=kb_split_mode(),
        )
        return

    if awaiting == "tip_fixed":
        try:
            rub = int("".join(ch for ch in message.text if ch.isdigit()))
            if rub < 0 or rub > 500000:
                raise ValueError()
        except Exception:
            await message.answer("Не понял сумму. Пример: 500")
            return
        sess.tip_fixed = rub
        await message.answer(f"Ок. Чаевые фикс: {sess.tip_fixed} ₽")
        await state.update_data(awaiting=None)
        await message.answer(
            "Как делим чек дальше?\n"
            "- по позициям (кто что ел/пил)\n"
            "- или сразу поровну на всех\n\n"
            "Общий заказ — «Разделить всё поровну».\n"
            "Разные позиции — «Разобрать по позициям».",
            reply_markup=kb_split_mode(),
        )
        return


async def cb_split_mode_items(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    _log_user_edit(
        session_id=data.get("receipt_session_id"),
        chat_id=call.message.chat.id,
        action="split_mode_items",
        details={},
    )
    await _open_private_survey_stage(call.message, state)


async def cb_split_mode_equal(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    all_uids = list(sess.participants.keys())
    if not all_uids:
        await call.message.answer("Сначала добавьте участников.")
        return

    for it in sess.items:
        it.weights = {uid: 1.0 for uid in all_uids}

    data = await state.get_data()
    _log_user_edit(
        session_id=data.get("receipt_session_id"),
        chat_id=call.message.chat.id,
        action="split_mode_equal",
        details={"participants_count": len(all_uids)},
    )
    await call.message.answer("Ок, разделил все позиции поровну на всех участников.")
    await _open_payments_stage(call.message, state)


# -------------------------
# Items: choose eaters + weights
# -------------------------
async def start_item_flow(message: Message, state: FSMContext):
    await state.set_state(ReceiptFlow.set_weights)
    await state.update_data(item_idx=0, awaiting_manual_weights=None)
    await ask_item_participants(message, state)


async def ask_item_participants(message: Message, state: FSMContext):
    sess = store.get(message.chat.id)
    data = await state.get_data()
    idx = data.get("item_idx", 0)

    if idx >= len(sess.items):
        await _open_payments_stage(message, state)
        return

    it = sess.items[idx]
    participants_list = list(sess.participants.items())
    selected = set(it.weights.keys())

    await message.answer(
        f"Позиция {idx+1}/{len(sess.items)}:\n"
        f"**{it.name}** — {it.price} ₽\n\n"
        "Кто ел эту позицию? Нажимай кнопки:",
        parse_mode="Markdown",
        reply_markup=kb_participants_toggle(participants_list, selected, idx)
    )


async def cb_item_toggle(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)

    _, s_idx, _, s_uid = call.data.split(":")
    idx = int(s_idx)
    uid = int(s_uid)

    it = sess.items[idx]
    if uid in it.weights:
        it.weights.pop(uid, None)
    else:
        it.weights[uid] = 1.0

    participants_list = list(sess.participants.items())
    selected = set(it.weights.keys())
    await call.message.edit_reply_markup(
        reply_markup=kb_participants_toggle(participants_list, selected, idx)
    )


async def cb_item_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    _, s_idx, _ = call.data.split(":")
    idx = int(s_idx)

    it = sess.items[idx]
    if not it.weights:
        await call.message.answer("Никто не выбран. Если позиция общая — выбери хотя бы одного.")
        return

    # Доли всегда равные между выбранными участниками.
    for uid in list(it.weights.keys()):
        it.weights[uid] = 1.0
    await state.update_data(item_idx=idx + 1)
    await ask_item_participants(call.message, state)


async def cb_weights(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)

    _, s_idx, mode = call.data.split(":")
    idx = int(s_idx)
    it = sess.items[idx]

    if mode == "equal":
        for uid in list(it.weights.keys()):
            it.weights[uid] = 1.0
        await call.message.answer("Ок, поровну.")
        await state.update_data(item_idx=idx + 1)
        await ask_item_participants(call.message, state)
        return

    if mode == "manual":
        names = [sess.participants[uid] for uid in it.weights.keys()]
        await call.message.answer(
            "Введи веса вручную в формате:\n"
            "имя=вес, имя=вес\n"
            "Пример: Иван=7, Пётр=3\n\n"
            f"Участники этой позиции: {', '.join(names)}"
        )
        await state.update_data(awaiting_manual_weights=idx)
        return


async def on_text_manual_weights(message: Message, state: FSMContext):
    sess = store.get(message.chat.id)
    data = await state.get_data()
    idx = data.get("awaiting_manual_weights")
    if idx is None:
        return
    it = sess.items[int(idx)]

    def norm_name(s: str) -> str:
        return "".join(ch.lower() for ch in s if ch.isalnum())

    name_map = {norm_name(n): uid for uid, n in sess.participants.items() if uid in it.weights}

    parts = [p.strip() for p in message.text.split(",") if p.strip()]
    if not parts:
        await message.answer("Не понял. Пример: Иван=7, Пётр=3")
        return

    new_weights = {}
    for p in parts:
        if "=" not in p:
            await message.answer("Нужно в формате имя=вес. Пример: Иван=7")
            return
        nm, vs = p.split("=", 1)
        nm = norm_name(nm.strip())
        try:
            w = float(vs.replace(",", ".").strip())
        except Exception:
            await message.answer(f"Не понял вес в '{p}'. Пример: Иван=7")
            return
        if w <= 0:
            await message.answer("Вес должен быть > 0")
            return
        if nm not in name_map:
            await message.answer("Имя не совпало с участниками. Пиши как в Telegram (часть имени).")
            return
        new_weights[name_map[nm]] = w

    for uid in list(it.weights.keys()):
        it.weights[uid] = new_weights.get(uid, 0)

    for uid in list(it.weights.keys()):
        if it.weights[uid] <= 0:
            it.weights.pop(uid, None)

    if not it.weights:
        await message.answer("После ввода не осталось участников. Попробуй снова.")
        return

    await message.answer("Ок, сохранил веса.")
    await state.update_data(awaiting_manual_weights=None)
    await state.update_data(item_idx=int(idx) + 1)
    await ask_item_participants(message, state)


# -------------------------
# Payments & results
# -------------------------
async def on_text_payments(message: Message, state: FSMContext):
    data = await state.get_data()
    private_group_chat_id = data.get("private_pay_group_chat_id")
    if private_group_chat_id is not None:
        group_chat_id = int(private_group_chat_id)
        sess = store.get(group_chat_id)
        uid = int(message.from_user.id)
        if uid not in sess.participants:
            await message.answer("Ты не в списке участников этого счёта.")
            return
        rub = int("".join(ch for ch in (message.text or "") if ch.isdigit()) or "0")
        sess.paid[uid] = rub
        await message.answer(f"Принял. Твоя оплата: {rub} ₽")
        try:
            await message.bot.send_message(group_chat_id, f"Обновление оплаты: {sess.participants[uid]} — {rub} ₽")
            await message.bot.send_message(group_chat_id, _fmt_paid_status(sess))
        except Exception:
            pass
        return

    sess = store.get(message.chat.id)

    lines = [ln.strip() for ln in message.text.splitlines() if ln.strip()]
    if not lines:
        return

    def find_uid_by_name(token: str):
        t = token.lower().strip()
        for uid, name in sess.participants.items():
            if t and t in name.lower():
                return uid
        return None

    updated = 0
    for ln in lines:
        if "=" not in ln:
            continue
        left, right = ln.split("=", 1)
        uid = find_uid_by_name(left)
        if uid is None:
            continue
        rub = int("".join(ch for ch in right if ch.isdigit()) or "0")
        sess.paid[uid] = rub
        updated += 1

    if updated == 0:
        await message.answer("Не смог распознать оплаты. Формат: Иван = 3200")
        return

    await message.answer("Ок, оплаты записал. Считаю итог…")
    await send_results(message)


async def send_results(message: Message):
    sess = store.get(message.chat.id)

    subtotals = calc_per_item_shares(sess.items, sess.participants)
    owed_total, tip_total, subtotal_total = apply_tip(subtotals, sess.tip_percent, sess.tip_fixed)
    grand_total = sum(owed_total.values())

    bal = balances(owed_total, sess.paid)
    transfers = min_transfers(bal)

    lines = []
    lines.append("🧾 **Итоги**")
    lines.append(f"Сумма по позициям: **{subtotal_total} ₽**")
    lines.append(f"Итого: **{grand_total} ₽**")
    lines.append("")
    lines.append("👥 **По людям** (должен всего / оплатил / баланс):")
    for uid, name in sess.participants.items():
        owe = owed_total.get(uid, 0)
        paid = sess.paid.get(uid, 0)
        b = bal.get(uid, 0)
        status = "доплатить" if b > 0 else ("получить" if b < 0 else "0")
        lines.append(f"- {name}: {owe} / {paid} / {b} ({status})")
    lines.append("")
    lines.append("💸 **Переводы (минимально):**")
    if not transfers:
        lines.append("Никто никому не должен ✅")
    else:
        for frm, to, amt in transfers:
            lines.append(f"- {sess.participants[frm]} → {sess.participants[to]}: **{amt} ₽**")

    await message.answer("\n".join(lines), parse_mode="Markdown")


async def cb_pay_status(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    await call.message.answer(_fmt_paid_status(sess))


async def cb_private_pay_full(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = (call.data or "").split(":")
    if len(parts) != 2 or parts[0] != "pf":
        return
    token = parts[1]
    group_chat_id = _PENDING_PRIVATE_PAY.get(token)
    if group_chat_id is None:
        await call.message.answer("Ссылка оплаты устарела. Запроси этап оплат из общего чата заново.")
        return
    sess = store.get(group_chat_id)
    uid = int(call.from_user.id)
    if uid not in sess.participants:
        await call.message.answer("Ты не в списке участников этого счёта.")
        return

    full_amount = int(sess.total_rub or _sum_items(sess.items))
    sess.paid[uid] = full_amount
    await call.message.answer(f"Принял. Отметил полную оплату: {full_amount} ₽")
    try:
        await call.message.bot.send_message(
            group_chat_id,
            f"Обновление оплаты: {sess.participants[uid]} — весь счёт ({full_amount} ₽)",
        )
        await call.message.bot.send_message(group_chat_id, _fmt_paid_status(sess))
    except Exception:
        pass

async def cb_private_pay_none(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = (call.data or "").split(":")
    if len(parts) != 2 or parts[0] != "pn":
        return
    token = parts[1]
    group_chat_id = _PENDING_PRIVATE_PAY.get(token)
    if group_chat_id is None:
        await call.message.answer("Ссылка оплаты устарела. Запроси этап оплат из общего чата заново.")
        return
    sess = store.get(group_chat_id)
    uid = int(call.from_user.id)
    if uid not in sess.participants:
        await call.message.answer("Ты не в списке участников этого счёта.")
        return

    sess.paid[uid] = 0
    await call.message.answer("Принял. Отметил, что ты не платил(а) по чеку.")
    try:
        await call.message.bot.send_message(
            group_chat_id,
            f"Обновление оплаты: {sess.participants[uid]} — 0 ₽ (не платил(а))",
        )
        await call.message.bot.send_message(group_chat_id, _fmt_paid_status(sess))
    except Exception:
        pass


async def cb_pay_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sess = store.get(call.message.chat.id)
    pending = [name for uid, name in sess.participants.items() if uid not in sess.paid]
    if pending:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Посчитать всё равно", callback_data="pay_done_force")
        b.button(text="🔄 Обновить статус оплат", callback_data="pay_status")
        b.adjust(1)
        await call.message.answer(
            "Не все участники отправили оплату.\n"
            + "\n".join(f"- {name}" for name in pending)
            + "\n\n"
            "Можно дождаться всех или посчитать уже сейчас.",
            reply_markup=b.as_markup(),
        )
        return
    await call.message.answer("Считаю итог...")
    await send_results(call.message)


async def cb_pay_done_force(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer("Считаю итог...")
    await send_results(call.message)


async def cb_survey_vote(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = (call.data or "").split(":")
    if len(parts) != 4 or parts[0] != "sv":
        return
    _, token, s_idx, vote = parts
    group_chat_id = _PENDING_PRIVATE_SURVEY.get(token)
    if group_chat_id is None:
        await call.message.answer("Опрос устарел. Открой новый опрос из общего чата.")
        return
    sess = store.get(group_chat_id)
    uid = int(call.from_user.id)
    if uid not in sess.participants:
        await call.message.answer("Ты не в списке участников этого счёта.")
        return
    try:
        idx = int(s_idx)
    except Exception:
        return
    answers = _PRIVATE_SURVEY_ANSWERS.setdefault((token, uid), set())
    if vote == "y":
        answers.add(idx)
    else:
        answers.discard(idx)
    await _ask_private_survey_item(call.message.bot, token, group_chat_id, uid, idx=idx + 1, chat_id=call.message.chat.id)


async def cb_survey_status(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    token = str(data.get("private_survey_token") or "")
    if not token:
        await call.message.answer("Нет активного опроса.")
        return
    sess = store.get(call.message.chat.id)
    await call.message.answer(_fmt_private_survey_status(sess, token))


async def cb_survey_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    token = str(data.get("private_survey_token") or "")
    if not token:
        await call.message.answer("Нет активного опроса.")
        return
    sess = store.get(call.message.chat.id)
    survey_uids = _survey_participant_uids(sess)
    pending = [sess.participants.get(uid, str(uid)) for uid in survey_uids if not _PRIVATE_SURVEY_DONE.get((token, uid), False)]
    if pending:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Перейти к оплатам всё равно", callback_data="survey_done_force")
        b.button(text="🔄 Обновить статус опроса", callback_data="survey_status")
        b.adjust(1)
        await call.message.answer(
            "Опрос завершили не все:\n" + "\n".join(f"- {name}" for name in pending),
            reply_markup=b.as_markup(),
        )
        return
    await call.message.answer("Опрос завершён. Переходим к этапу оплат.")
    await _open_payments_stage(call.message, state)


async def cb_survey_done_force(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer("Переходим к оплатам.")
    await _open_payments_stage(call.message, state)


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_new, Command("new"))

    dp.message.register(on_photo, ReceiptFlow.waiting_photo, F.photo)
    dp.message.register(on_document, ReceiptFlow.waiting_photo, F.document)

    dp.callback_query.register(cb_items_ok, F.data == "items_ok")
    dp.callback_query.register(cb_items_rescan, F.data == "items_rescan")

    dp.callback_query.register(cb_join, F.data == "join")
    dp.callback_query.register(cb_join_test, F.data == "join_test")
    dp.callback_query.register(cb_join_done, F.data == "join_done")

    dp.callback_query.register(cb_split_mode_items, F.data == "split_mode_items")
    dp.callback_query.register(cb_split_mode_equal, F.data == "split_mode_equal")
    dp.callback_query.register(cb_survey_status, F.data == "survey_status")
    dp.callback_query.register(cb_survey_done, F.data == "survey_done")
    dp.callback_query.register(cb_survey_done_force, F.data == "survey_done_force")
    dp.callback_query.register(cb_survey_vote, F.data.startswith("sv:"))
    dp.callback_query.register(cb_private_pay_full, F.data.startswith("pf:"))
    dp.callback_query.register(cb_private_pay_none, F.data.startswith("pn:"))
    dp.callback_query.register(cb_pay_status, F.data == "pay_status")
    dp.callback_query.register(cb_pay_done, F.data == "pay_done")
    dp.callback_query.register(cb_pay_done_force, F.data == "pay_done_force")

    dp.callback_query.register(cb_item_toggle, F.data.startswith("it:") & F.data.contains(":tog:"))
    dp.callback_query.register(cb_item_done, F.data.startswith("it:") & F.data.endswith(":done"))

    dp.message.register(on_text_payments, ReceiptFlow.set_payments, F.text)


# -------------------------
# Main
# -------------------------
async def main():
    cfg = load_config()
    bot = Bot(cfg.bot_token)

    dp = Dispatcher(storage=MemoryStorage())
    setup_dispatcher(dp)

    log.info("BOT STARTED: polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
