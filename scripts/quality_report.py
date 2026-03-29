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
EDITS_LOG = LOGS_DIR / "receipt_user_edits.jsonl"


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


def _filter_since(rows: list[dict[str, Any]], since_utc: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = _parse_ts(str(r.get("timestamp", "")))
        if ts and ts >= since_utc:
            out.append(r)
    return out


def _fmt_counter(c: Counter, limit: int = 10) -> str:
    if not c:
        return "- (пусто)"
    lines: list[str] = []
    for k, v in c.most_common(limit):
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def build_report(days: int) -> str:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    sessions_all = _read_jsonl(SESSIONS_LOG)
    edits_all = _read_jsonl(EDITS_LOG)
    sessions = _filter_since(sessions_all, since)
    edits = _filter_since(edits_all, since)

    status_ctr: Counter[str] = Counter()
    reason_ctr: Counter[str] = Counter()
    mode_ctr: Counter[str] = Counter()
    action_ctr: Counter[str] = Counter()

    synth_vals: list[float] = []
    diff_vals: list[int] = []

    for s in sessions:
        status = str(s.get("quality_status") or "unknown")
        mode = str(s.get("mode_used") or "unknown")
        status_ctr[status] += 1
        mode_ctr[mode] += 1

        reasons = s.get("quality_reasons") or []
        if isinstance(reasons, list):
            for r in reasons:
                reason_ctr[str(r)] += 1

        metrics = s.get("metrics") or {}
        if isinstance(metrics, dict):
            sr = metrics.get("synthetic_ratio")
            if isinstance(sr, (int, float)):
                synth_vals.append(float(sr))
            diff = metrics.get("diff_rub")
            if isinstance(diff, int):
                diff_vals.append(abs(diff))

    for e in edits:
        action = str(e.get("action") or "unknown")
        action_ctr[action] += 1

    avg_synth = (sum(synth_vals) / len(synth_vals)) if synth_vals else 0.0
    avg_abs_diff = (sum(diff_vals) / len(diff_vals)) if diff_vals else 0.0

    lines: list[str] = []
    lines.append(f"Quality weekly summary (last {days} days)")
    lines.append(f"Period UTC: {since.isoformat()} .. {now.isoformat()}")
    lines.append("")
    lines.append(f"Sessions analyzed: {len(sessions)}")
    lines.append(f"Edits analyzed: {len(edits)}")
    lines.append(f"Avg synthetic ratio: {avg_synth:.2%}")
    lines.append(f"Avg |diff_rub|: {avg_abs_diff:.1f}")
    lines.append("")
    lines.append("Status breakdown:")
    lines.append(_fmt_counter(status_ctr))
    lines.append("")
    lines.append("Mode breakdown:")
    lines.append(_fmt_counter(mode_ctr))
    lines.append("")
    lines.append("Top quality reasons:")
    lines.append(_fmt_counter(reason_ctr))
    lines.append("")
    lines.append("Top user edit actions:")
    lines.append(_fmt_counter(action_ctr))
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggregate bot quality/edit logs into a short report")
    ap.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    ap.add_argument("--out", default=None, help="Optional output file path")
    args = ap.parse_args()

    days = max(1, int(args.days))
    report = build_report(days=days)

    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"Report saved: {out}")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
