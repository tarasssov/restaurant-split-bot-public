from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
SESSIONS_LOG = LOGS_DIR / "receipt_sessions.jsonl"


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


def _classify(session: dict[str, Any]) -> tuple[str, list[str]]:
    status = str(session.get("quality_status") or "unknown")
    reasons = [str(x) for x in (session.get("quality_reasons") or [])]
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

    if flags:
        return "FAIL", flags

    review_flags: list[str] = []
    if status == "warning":
        review_flags.append("quality_status=warning")
    if synth_ratio >= 0.10:
        review_flags.append(f"synthetic={synth_ratio:.1%}")
    for r in reasons:
        if r in {"dominant_item", "merged_items_warning", "suspicious_price"}:
            review_flags.append(f"reason={r}")

    if review_flags:
        return "REVIEW", review_flags
    return "PASS", []


def build_report(days: int, limit: int, chat_id: int | None) -> str:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    sessions = _read_jsonl(SESSIONS_LOG)

    filtered: list[dict[str, Any]] = []
    for s in sessions:
        if s.get("event") != "receipt_session":
            continue
        ts = _parse_ts(str(s.get("timestamp", "")))
        if not ts or ts < since:
            continue
        if chat_id is not None and int(s.get("chat_id") or 0) != chat_id:
            continue
        filtered.append(s)

    filtered.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    recent = filtered[: max(1, limit)]

    verdict_ctr: Counter[str] = Counter()
    lines: list[str] = []
    lines.append(f"Receipt auto-check (last {days} days, latest {len(recent)} sessions)")
    lines.append(f"Period UTC: {since.isoformat()} .. {now.isoformat()}")
    if chat_id is not None:
        lines.append(f"chat_id filter: {chat_id}")
    lines.append("")

    for s in recent:
        verdict, flags = _classify(s)
        verdict_ctr[verdict] += 1
        sid = str(s.get("session_id") or "-")
        mode = str(s.get("mode_used") or "unknown")
        status = str(s.get("quality_status") or "unknown")
        ts = str(s.get("timestamp") or "-")
        metrics = s.get("metrics") or {}
        synth_ratio = float(metrics.get("synthetic_ratio") or 0.0)
        items_count = int(metrics.get("items_count") or 0)
        lines.append(
            f"[{verdict}] {ts} | sid={sid} | mode={mode} | status={status} | synth={synth_ratio:.1%} | items={items_count}"
        )
        if flags:
            lines.append("  flags: " + "; ".join(flags))

    lines.append("")
    lines.append("Summary:")
    lines.append(f"- PASS: {verdict_ctr.get('PASS', 0)}")
    lines.append(f"- REVIEW: {verdict_ctr.get('REVIEW', 0)}")
    lines.append(f"- FAIL: {verdict_ctr.get('FAIL', 0)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-check recent receipt sessions with PASS/REVIEW/FAIL verdict")
    ap.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    ap.add_argument("--limit", type=int, default=20, help="How many latest sessions to print (default: 20)")
    ap.add_argument("--chat-id", type=int, default=None, help="Optional chat_id filter")
    ap.add_argument("--out", default=None, help="Optional output file path")
    args = ap.parse_args()

    report = build_report(days=max(1, int(args.days)), limit=max(1, int(args.limit)), chat_id=args.chat_id)
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"Report saved: {out}")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
