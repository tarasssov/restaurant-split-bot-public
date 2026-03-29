from __future__ import annotations

import argparse
from pathlib import Path
import re

from app.receipt_parser import parse_receipt_text, extract_total_rub


FIXTURES_DIR = Path("fixtures")


def _synthetic_amount(items) -> int:
    out = 0
    for it in items:
        if "корректировка по итогу" in (it.name or "").lower():
            out += int(it.price)
    return out


def _suspicious_items_count(items) -> int:
    cnt = 0
    for it in items:
        n = (it.name or "").strip()
        if not n:
            cnt += 1
            continue
        if len(re.findall(r"[A-Za-zА-Яа-яЁё]", n)) < 2:
            cnt += 1
            continue
        if re.fullmatch(r"[\W_]+", n):
            cnt += 1
            continue
    return cnt


def run_one(path: Path, *, max_synthetic_ratio: float | None = None) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    items = parse_receipt_text(text)

    total = extract_total_rub(text)
    s = sum(i.price for i in items)
    diff = (total - s) if total is not None else None
    synthetic = _synthetic_amount(items)
    synthetic_ratio = (abs(synthetic) / abs(total)) if total else 0.0
    suspicious = _suspicious_items_count(items)

    print(f"\n=== TEST: {path.name} ===")
    print(f"TOTAL : {total}")
    print(f"SUM   : {s}")
    print(f"DIFF  : {diff}")
    print(f"ITEMS : {len(items)}")
    print(f"SYNTH : {synthetic} ({synthetic_ratio:.1%})")
    print(f"SUSP  : {suspicious}")
    print("\n--- ITEMS ---")
    for it in items:
        print(f"{it.price:6d} | {it.name}")

    ok = True
    if total is not None and s != total:
        ok = False
        print("❌ FAIL: SUM != TOTAL")
    if max_synthetic_ratio is not None and synthetic_ratio > max_synthetic_ratio:
        ok = False
        print(f"❌ FAIL: synthetic_ratio {synthetic_ratio:.1%} > {max_synthetic_ratio:.1%}")
    else:
        print("✅ OK")
    return ok


def iter_fixture_files() -> list[Path]:
    if not FIXTURES_DIR.exists():
        return []

    files: list[Path] = []
    for p in sorted(FIXTURES_DIR.glob("*.txt")):
        # Skip helper outputs / logs that are not fixtures
        if p.name.startswith("_"):
            continue
        if p.name.lower() in {"readme.txt", "readme.md"}:
            continue
        if p.name.endswith("_output.txt") or p.name.endswith("_last_test_output.txt"):
            continue
        files.append(p)
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--one", help="Run a single fixture by file name (in fixtures/)", default=None)
    ap.add_argument(
        "--max-synthetic-ratio",
        type=float,
        default=None,
        help="Fail if abs(synthetic)/total exceeds this threshold (for example 0.2)",
    )
    args = ap.parse_args()

    if args.one:
        p = FIXTURES_DIR / args.one
        if not p.exists():
            raise SystemExit(f"Fixture not found: {p}")
        run_one(p, max_synthetic_ratio=args.max_synthetic_ratio)
        return

    files = iter_fixture_files()
    if not files:
        print("No fixtures found in ./fixtures")
        return

    ok_all = True
    for p in files:
        ok_all = run_one(p, max_synthetic_ratio=args.max_synthetic_ratio) and ok_all

    if not ok_all:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
