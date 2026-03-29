from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9 runtime fallback
    from backports.zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
SESSIONS_LOG = LOGS_DIR / "receipt_sessions.jsonl"
STATE_PATH_DEFAULT = LOGS_DIR / "quality_reports" / "alert_state.json"


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return bool(default)
    return v in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v in (None, "", "None"):
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


def _resolve_alert_bot_token() -> str:
    return (os.getenv("QUALITY_ALERT_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()


def _resolve_project_name() -> str:
    return (
        (os.getenv("QUALITY_ALERT_PROJECT_NAME") or "").strip()
        or (os.getenv("APP_NAME") or "").strip()
        or ROOT.name
    )


def _parse_ts(v: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(v)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_newer(ts: datetime, sid: str, *, last_ts: datetime | None, last_sid: str | None) -> bool:
    if last_ts is None:
        return True
    if ts > last_ts:
        return True
    if ts < last_ts:
        return False
    return sid > (last_sid or "")


def _auto_fail_flags(session: dict[str, Any]) -> list[str]:
    status = str(session.get("quality_status") or "unknown")
    metrics = session.get("metrics") or {}
    synth_ratio = float(metrics.get("synthetic_ratio") or 0.0)
    diff_rub = int(metrics.get("diff_rub") or 0)
    items_count = int(metrics.get("items_count") or 0)

    flags: list[str] = []
    if status == "low_confidence":
        flags.append("quality_status=low_confidence")
    if abs(diff_rub) > 0:
        flags.append(f"diff_rub={diff_rub}")
    if synth_ratio > 0.25:
        flags.append(f"synthetic={synth_ratio:.1%}")
    if items_count < 3:
        flags.append(f"items_count={items_count}")
    return flags


@dataclass
class AlertDecision:
    send: bool
    message: str
    new_sessions_count: int
    new_problem_count: int
    unsent_problem_count: int
    last_seen_ts: str | None
    last_seen_sid: str | None
    sent_problem_session_ids: list[str]


def _build_alert_message(
    *,
    problems: list[dict[str, Any]],
    tz_name: str,
    min_new: int,
    project_name: str,
) -> str:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
    reason_ctr: Counter[str] = Counter()
    for s in problems:
        reasons = s.get("quality_reasons") or []
        if isinstance(reasons, list):
            for r in reasons:
                reason_ctr[str(r)] += 1
        for f in _auto_fail_flags(s):
            reason_ctr[f"auto:{f}"] += 1

    top_reasons = reason_ctr.most_common(8)
    lines: list[str] = []
    lines.append("Weekly quality alert")
    lines.append(f"Project: {project_name}")
    lines.append(f"Time: {now_local}")
    lines.append(f"New problematic receipts: {len(problems)} (threshold={min_new})")
    lines.append("")
    lines.append("Top reasons:")
    if top_reasons:
        for k, v in top_reasons:
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Latest 5 session_id:")
    for s in problems[-5:]:
        lines.append(f"- {s.get('session_id')}")
    lines.append("")
    lines.append("Debug commands:")
    lines.append("python3 scripts/receipt_auto_check.py --days 7 --limit 20")
    lines.append("python3 scripts/export_receipt_dataset.py --days 30 --status low_confidence --limit 20")
    lines.append("python3 scripts/replay_ocr_sessions.py --days 30 --reparse --group-by-ocr-hash")
    return "\n".join(lines)


def _send_telegram_message(
    bot_token: str,
    chat_id: int,
    text: str,
    *,
    proxy_url: str | None = None,
) -> tuple[bool, str]:
    def _sanitize(msg: str) -> str:
        out = str(msg or "")
        if bot_token:
            out = out.replace(bot_token, "<redacted>")
        out = re.sub(r"/bot\d+:[A-Za-z0-9_\-]+", "/bot<redacted>", out)
        return out

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    req_kwargs: dict[str, Any] = {"json": payload, "timeout": 25}
    if proxy_url:
        req_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.post(url, **req_kwargs)
    except Exception as e:
        return False, _sanitize(f"{type(e).__name__}: {e}")
    if resp.status_code != 200:
        return False, _sanitize(f"HTTP {resp.status_code}: {resp.text[:400]}")
    return True, "ok"


def evaluate(
    *,
    state_path: Path,
    min_new_fails: int,
    force_send: bool,
    tz_name: str,
    project_name: str = ROOT.name,
) -> AlertDecision:
    rows = [r for r in _read_jsonl(SESSIONS_LOG) if r.get("event") == "receipt_session"]
    rows.sort(key=lambda x: (str(x.get("timestamp") or ""), str(x.get("session_id") or "")))

    state = _load_state(state_path)
    last_seen_ts = _parse_ts(str(state.get("last_seen_ts") or "")) if state else None
    last_seen_sid = str(state.get("last_seen_sid") or "") if state else ""
    sent_ids = set(str(x) for x in (state.get("sent_problem_session_ids") or []) if str(x))

    new_sessions: list[dict[str, Any]] = []
    for s in rows:
        ts = _parse_ts(str(s.get("timestamp") or ""))
        if ts is None:
            continue
        sid = str(s.get("session_id") or "")
        if _is_newer(ts, sid, last_ts=last_seen_ts, last_sid=last_seen_sid):
            new_sessions.append(s)

    if new_sessions:
        new_last_seen_ts = str(new_sessions[-1].get("timestamp") or "")
        new_last_seen_sid = str(new_sessions[-1].get("session_id") or "")
    else:
        new_last_seen_ts = state.get("last_seen_ts")
        new_last_seen_sid = state.get("last_seen_sid")

    new_problems: list[dict[str, Any]] = []
    for s in new_sessions:
        status = str(s.get("quality_status") or "")
        is_low_conf = status == "low_confidence"
        is_fail = len(_auto_fail_flags(s)) > 0
        if is_low_conf or is_fail:
            new_problems.append(s)

    unsent = [s for s in new_problems if str(s.get("session_id") or "") not in sent_ids]
    should_send = force_send or (len(unsent) >= max(1, min_new_fails))

    sent_after = list(sent_ids)
    if should_send:
        for s in unsent:
            sid = str(s.get("session_id") or "")
            if sid:
                sent_after.append(sid)
    sent_after = sent_after[-2000:]

    msg = _build_alert_message(
        problems=unsent,
        tz_name=tz_name,
        min_new=max(1, min_new_fails),
        project_name=project_name,
    )
    if not unsent and not force_send:
        msg = (
            f"Weekly quality alert\nProject: {project_name}\n"
            "No new FAIL/low_confidence sessions since last watermark."
        )

    return AlertDecision(
        send=should_send,
        message=msg,
        new_sessions_count=len(new_sessions),
        new_problem_count=len(new_problems),
        unsent_problem_count=len(unsent),
        last_seen_ts=str(new_last_seen_ts) if new_last_seen_ts else None,
        last_seen_sid=str(new_last_seen_sid) if new_last_seen_sid else None,
        sent_problem_session_ids=sent_after,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly quality alert checker for problematic receipt sessions")
    ap.add_argument("--dry-run", action="store_true", help="Do not send Telegram message and do not update state")
    ap.add_argument("--force-send", action="store_true", help="Send even if no new problematic sessions")
    ap.add_argument("--state-file", default=str(STATE_PATH_DEFAULT), help="State file path")
    args = ap.parse_args()

    enabled = _env_bool("QUALITY_ALERT_ENABLED", True)
    tz_name = os.getenv("QUALITY_ALERT_TZ") or "Europe/Moscow"
    min_new_fails = max(1, _env_int("QUALITY_ALERT_MIN_NEW_FAILS", 1))
    state_path = Path(args.state_file).expanduser().resolve()
    project_name = _resolve_project_name()

    chat_id_raw = (os.getenv("QUALITY_ALERT_CHAT_ID") or os.getenv("ADMIN_USER_ID") or "").strip()
    bot_token = _resolve_alert_bot_token()
    proxy_url = (os.getenv("QUALITY_ALERT_PROXY_URL") or "").strip() or None
    chat_id = int(chat_id_raw) if chat_id_raw.lstrip("-").isdigit() else 0

    decision = evaluate(
        state_path=state_path,
        min_new_fails=min_new_fails,
        force_send=args.force_send,
        tz_name=tz_name,
        project_name=project_name,
    )

    print(
        f"quality_alert_check: new_sessions={decision.new_sessions_count} "
        f"new_problems={decision.new_problem_count} unsent_problems={decision.unsent_problem_count} send={decision.send}"
    )
    print("---")
    print(decision.message)

    if args.dry_run:
        print("dry-run: state not updated, telegram not sent")
        return

    if not enabled and not args.force_send:
        print("quality_alert_check: disabled by QUALITY_ALERT_ENABLED")
        return

    if decision.send:
        if not bot_token:
            raise SystemExit("QUALITY_ALERT_BOT_TOKEN (or BOT_TOKEN) is required for Telegram alert delivery")
        if not chat_id:
            raise SystemExit("QUALITY_ALERT_CHAT_ID (or ADMIN_USER_ID) is required for Telegram alert delivery")
        ok, details = _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=decision.message,
            proxy_url=proxy_url,
        )
        if not ok:
            raise SystemExit(f"Telegram alert send failed: {details}")
        print("Telegram alert sent.")
    else:
        print("No alert sent: no new problematic sessions above threshold.")

    state_payload = {
        "last_seen_ts": decision.last_seen_ts,
        "last_seen_sid": decision.last_seen_sid,
        "sent_problem_session_ids": decision.sent_problem_session_ids,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state_path, state_payload)
    print(f"State updated: {state_path}")


if __name__ == "__main__":
    main()
