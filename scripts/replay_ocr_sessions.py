from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import _quality_status
from app.receipt_parser import extract_total_rub, parse_receipt_text_with_variant
from app.storage import Item


LOGS_DIR = ROOT / "logs"
SESSIONS_LOG = LOGS_DIR / "receipt_sessions.jsonl"
OCR_DIR = LOGS_DIR / "ocr_sessions"


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


def _filter_days(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("event") != "receipt_session":
            continue
        ts = _parse_ts(str(r.get("timestamp") or ""))
        if ts and ts >= since:
            out.append(r)
    return out


def _fmt_counter(c: Counter[str], *, limit: int = 20) -> str:
    if not c:
        return "- (empty)"
    return "\n".join(f"- {k}: {v}" for k, v in c.most_common(limit))


def _load_ocr_text(session: dict[str, Any]) -> str | None:
    p_raw = session.get("ocr_text_path")
    if isinstance(p_raw, str) and p_raw.strip():
        p = Path(p_raw)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="ignore")
    sid = str(session.get("session_id") or "")
    if sid:
        p = OCR_DIR / f"{sid}.txt"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="ignore")
    return None


def _baseline_block(rows: list[dict[str, Any]], label: str) -> str:
    status_ctr: Counter[str] = Counter()
    reason_ctr: Counter[str] = Counter()
    mode_ctr: Counter[str] = Counter()
    for r in rows:
        status_ctr[str(r.get("quality_status") or "unknown")] += 1
        mode_ctr[str(r.get("mode_used") or "unknown")] += 1
        reasons = r.get("quality_reasons") or []
        if isinstance(reasons, list):
            for reason in reasons:
                reason_ctr[str(reason)] += 1
    lines: list[str] = []
    lines.append(f"[baseline {label}] sessions={len(rows)}")
    lines.append("status:")
    lines.append(_fmt_counter(status_ctr))
    lines.append("mode:")
    lines.append(_fmt_counter(mode_ctr))
    lines.append("reasons:")
    lines.append(_fmt_counter(reason_ctr))
    return "\n".join(lines)


def _baseline_data(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_ctr: Counter[str] = Counter()
    reason_ctr: Counter[str] = Counter()
    mode_ctr: Counter[str] = Counter()
    for r in rows:
        status_ctr[str(r.get("quality_status") or "unknown")] += 1
        mode_ctr[str(r.get("mode_used") or "unknown")] += 1
        reasons = r.get("quality_reasons") or []
        if isinstance(reasons, list):
            for reason in reasons:
                reason_ctr[str(reason)] += 1
    return {
        "sessions": len(rows),
        "status": dict(status_ctr),
        "mode": dict(mode_ctr),
        "reasons": dict(reason_ctr),
    }


def _status_rank(status: str) -> int:
    if status == "good":
        return 0
    if status == "warning":
        return 1
    return 2


def replay(rows: list[dict[str, Any]], *, group_by_ocr_hash: bool) -> tuple[str, dict[str, Any]]:
    changed_ctr: Counter[str] = Counter()
    old_status_ctr: Counter[str] = Counter()
    new_status_ctr: Counter[str] = Counter()
    replayed = 0
    skipped = 0
    group_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "old_status": Counter(),
        "new_status": Counter(),
        "sample_session_ids": [],
    })

    for s in rows:
        old_status = str(s.get("quality_status") or "unknown")
        old_status_ctr[old_status] += 1
        txt = _load_ocr_text(s)
        if not txt:
            skipped += 1
            continue
        parsed, _ = parse_receipt_text_with_variant(txt)
        total_rub = extract_total_rub(txt)
        items = [Item(name=i.name, price=int(i.price)) for i in parsed]
        new_status = _quality_status(items, total_rub)
        new_status_ctr[new_status] += 1
        replayed += 1

        if _status_rank(new_status) < _status_rank(old_status):
            changed_ctr["improved"] += 1
        elif _status_rank(new_status) > _status_rank(old_status):
            changed_ctr["regressed"] += 1
        else:
            changed_ctr["unchanged"] += 1

        if group_by_ocr_hash:
            ocr_hash = str(s.get("ocr_hash") or "")
            if not ocr_hash:
                ocr_hash = hashlib.sha1(txt.encode("utf-8")).hexdigest()[:24]
            grp = group_stats[ocr_hash]
            grp["count"] = int(grp["count"]) + 1
            grp["old_status"][old_status] += 1
            grp["new_status"][new_status] += 1
            if len(grp["sample_session_ids"]) < 5:
                grp["sample_session_ids"].append(str(s.get("session_id") or ""))

    lines: list[str] = []
    lines.append(f"[replay] replayed={replayed} skipped_missing_ocr={skipped}")
    lines.append("old_status:")
    lines.append(_fmt_counter(old_status_ctr))
    lines.append("new_status:")
    lines.append(_fmt_counter(new_status_ctr))
    lines.append("delta:")
    lines.append(_fmt_counter(changed_ctr))

    top_groups: list[dict[str, Any]] = []
    if group_by_ocr_hash:
        lines.append("top repeating ocr_hash families:")
        ranked = sorted(group_stats.items(), key=lambda kv: int(kv[1]["count"]), reverse=True)
        for h, g in ranked[:10]:
            old_low = int(g["old_status"].get("low_confidence", 0))
            new_low = int(g["new_status"].get("low_confidence", 0))
            lines.append(
                f"- {h}: count={g['count']} old_low={old_low} new_low={new_low} samples={','.join(g['sample_session_ids'])}"
            )
            top_groups.append(
                {
                    "ocr_hash": h,
                    "count": int(g["count"]),
                    "old_status": dict(g["old_status"]),
                    "new_status": dict(g["new_status"]),
                    "sample_session_ids": list(g["sample_session_ids"]),
                }
            )

    data = {
        "replayed": replayed,
        "skipped_missing_ocr": skipped,
        "old_status": dict(old_status_ctr),
        "new_status": dict(new_status_ctr),
        "delta": dict(changed_ctr),
        "top_ocr_hash_groups": top_groups,
    }
    return "\n".join(lines), data


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay OCR snapshots and compare quality status with current parser")
    ap.add_argument("--days", type=int, default=30, help="Replay window in days (default: 30)")
    ap.add_argument("--baseline-days", type=int, default=30, help="Long baseline window in days (default: 30)")
    ap.add_argument("--short-baseline-days", type=int, default=7, help="Short baseline window in days (default: 7)")
    ap.add_argument("--reparse", action="store_true", help="Run replay parser over OCR snapshots")
    ap.add_argument("--group-by-ocr-hash", action="store_true", help="Group replay stats by OCR hash family")
    ap.add_argument("--out", default=None, help="Optional JSON output path")
    args = ap.parse_args()

    rows = _read_jsonl(SESSIONS_LOG)
    baseline_long = _filter_days(rows, max(1, int(args.baseline_days)))
    baseline_short = _filter_days(rows, max(1, int(args.short_baseline_days)))
    replay_rows = _filter_days(rows, max(1, int(args.days)))

    lines: list[str] = []
    lines.append(_baseline_block(baseline_long, f"{max(1, int(args.baseline_days))}d"))
    lines.append("")
    lines.append(_baseline_block(baseline_short, f"{max(1, int(args.short_baseline_days))}d"))

    data: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_30d": _baseline_data(baseline_long),
        "baseline_7d": _baseline_data(baseline_short),
    }

    if args.reparse:
        lines.append("")
        replay_text, replay_data = replay(
            replay_rows,
            group_by_ocr_hash=bool(args.group_by_ocr_hash),
        )
        lines.append(replay_text)
        data["replay"] = replay_data

    report = "\n".join(lines) + "\n"
    print(report, end="")

    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON summary saved: {out}")


if __name__ == "__main__":
    main()
