from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
SESSIONS_LOG = LOGS_DIR / "receipt_sessions.jsonl"
EDITS_LOG = LOGS_DIR / "receipt_user_edits.jsonl"
OCR_DIR = LOGS_DIR / "ocr_sessions"
DATASETS_DIR = LOGS_DIR / "datasets"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _parse_ts(v: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(v)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_name(v: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (v or ""))
    out = out.strip("_")
    return out or "unknown"


def _find_ocr_text(session: dict[str, Any]) -> tuple[str | None, str | None]:
    path_raw = session.get("ocr_text_path")
    if isinstance(path_raw, str) and path_raw.strip():
        p = Path(path_raw.strip())
        if p.exists():
            return str(p), p.read_text(encoding="utf-8", errors="ignore")
    sid = str(session.get("session_id") or "")
    if sid:
        fallback = OCR_DIR / f"{sid}.txt"
        if fallback.exists():
            return str(fallback), fallback.read_text(encoding="utf-8", errors="ignore")
    return None, None


def export_dataset(days: int, status: str, out_dir: Path, limit: int | None) -> Path:
    sessions = _read_jsonl(SESSIONS_LOG)
    edits = _read_jsonl(EDITS_LOG)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    selected: list[dict[str, Any]] = []
    for s in sessions:
        if s.get("event") != "receipt_session":
            continue
        if str(s.get("quality_status") or "") != status:
            continue
        ts = _parse_ts(str(s.get("timestamp") or ""))
        if not ts or ts < since:
            continue
        selected.append(s)

    selected.sort(key=lambda x: str(x.get("timestamp") or ""))
    if limit is not None and limit > 0:
        selected = selected[-limit:]

    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = out_dir / f"{status}_{days}d_{ts_tag}"
    cases_dir = root / "cases"
    ocr_dir = root / "ocr_texts"
    cases_dir.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)

    edits_by_sid: dict[str, list[dict[str, Any]]] = {}
    for e in edits:
        sid = str(e.get("session_id") or "")
        if sid:
            edits_by_sid.setdefault(sid, []).append(e)

    index_rows: list[dict[str, Any]] = []
    for i, s in enumerate(selected, start=1):
        sid = str(s.get("session_id") or "")
        sid_safe = _safe_name(sid)
        case_id = f"{i:03d}_{sid_safe}"

        ocr_path, ocr_text = _find_ocr_text(s)
        ocr_out = None
        if ocr_text is not None:
            ocr_out_path = ocr_dir / f"{case_id}.txt"
            ocr_out_path.write_text(ocr_text, encoding="utf-8")
            ocr_out = str(ocr_out_path)

        payload = {
            "case_id": case_id,
            "session": s,
            "user_edits": edits_by_sid.get(sid, []),
            "ocr_text_export_path": ocr_out,
            "ocr_text_original_path": ocr_path,
        }
        case_json = cases_dir / f"{case_id}.json"
        case_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        index_rows.append(
            {
                "case_id": case_id,
                "timestamp": s.get("timestamp"),
                "session_id": sid,
                "chat_id": s.get("chat_id"),
                "mode_used": s.get("mode_used"),
                "quality_status": s.get("quality_status"),
                "quality_reasons": s.get("quality_reasons"),
                "metrics": s.get("metrics"),
                "case_json": str(case_json),
                "ocr_text": ocr_out,
            }
        )

    (root / "index.json").write_text(json.dumps(index_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "filter": {"days": days, "status": status, "limit": limit},
        "total_cases": len(index_rows),
        "output_dir": str(root),
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def main() -> None:
    ap = argparse.ArgumentParser(description="Export receipt sessions into a tuning dataset")
    ap.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    ap.add_argument(
        "--status",
        default="low_confidence",
        choices=["good", "warning", "low_confidence"],
        help="Which quality_status to export (default: low_confidence)",
    )
    ap.add_argument("--limit", type=int, default=None, help="Optional max number of latest cases")
    ap.add_argument("--out-dir", default=str(DATASETS_DIR), help="Base output dir (default: logs/datasets)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out = export_dataset(
        days=max(1, int(args.days)),
        status=str(args.status),
        out_dir=out_dir,
        limit=(None if args.limit is None else max(1, int(args.limit))),
    )
    print(f"Dataset exported: {out}")


if __name__ == "__main__":
    main()
