from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from app.ocr import extract_text
from app.receipt_parser import parse_receipt_text, extract_total_rub
from app.llm import llm_parse_receipt
from app.llm_refiner import llm_refine_receipt_items, LLMResult

LOGS_DIR = ROOT / "logs"
TEXTS_DIR = LOGS_DIR / "yandex_ocr_texts"
DEFAULT_REPORT = LOGS_DIR / "yandex_ocr_report.txt"
BASELINE_DIR = LOGS_DIR / "baselines"
SCRIPT_VERSION = "yandex_ocr_test_v5"
LOW_CONF_SYNTH_RATIO = 0.25
SOFT_FALLBACK_SYNTH_RATIO = 0.20

# Public repo intentionally does not ship real receipt photos.
# Pass one or more --image arguments to run an integration check on your own samples.
DEFAULT_IMAGES: list[Path] = []


def _synthetic_amount(items) -> int:
    total = 0
    for it in items:
        if "корректировка по итогу" in (it.name or "").lower():
            total += int(it.price)
    return total


def _non_adjust_items(items):
    return [it for it in items if "корректировка по итогу" not in (it.name or "").lower()]


_ALCOHOL_HINTS = (
    "водка", "вино", "виски", "ром", "джин", "ликер", "коньяк", "бренди",
    "текила", "шампан", "сидр", "пиво", "настойка",
)


def _is_alcohol_item_name(name: str) -> bool:
    low = (name or "").lower()
    return any(k in low for k in _ALCOHOL_HINTS)


def _max_item_share(items, total: int | None) -> float:
    if not total:
        return 0.0
    base = _non_adjust_items(items)
    if not base:
        return 0.0
    mx = max(int(i.price) for i in base)
    return mx / max(1, abs(int(total)))


def _max_non_alcohol_item_share(items, total: int | None) -> float:
    if not total:
        return 0.0
    base = [i for i in _non_adjust_items(items) if not _is_alcohol_item_name(i.name)]
    if not base:
        return 0.0
    mx = max(int(i.price) for i in base)
    return mx / max(1, abs(int(total)))


def _dominant_item_threshold(total: int | None) -> float:
    if total is None:
        return 0.45
    t = abs(int(total))
    if t < 2500:
        return 0.60
    if t < 4000:
        return 0.52
    return 0.45


def _semantic_fail(items, total: int | None) -> bool:
    base = _non_adjust_items(items)
    if len(base) < 3:
        return True
    if total is not None:
        synth_ratio = abs(_synthetic_amount(items)) / max(1, abs(int(total)))
        if synth_ratio > LOW_CONF_SYNTH_RATIO:
            return True
    if total is not None and _max_non_alcohol_item_share(items, total) > _dominant_item_threshold(total):
        return True
    return False


def _quality_score(items, total: int | None) -> float:
    if total is None:
        return float(_semantic_fail(items, total))
    diff = abs(int(total) - sum(int(i.price) for i in items)) / max(1, abs(int(total)))
    synth = abs(_synthetic_amount(items)) / max(1, abs(int(total)))
    sem = 0.5 if _semantic_fail(items, total) else 0.0
    return diff + synth + sem


def _is_low_conf(items, total: int | None) -> bool:
    if total is None:
        return _semantic_fail(items, total)
    synth = abs(_synthetic_amount(items)) / max(1, abs(int(total)))
    return synth > LOW_CONF_SYNTH_RATIO or _semantic_fail(items, total)


def _accept_candidate(cur_items, cand_items, total: int | None) -> bool:
    cur_score = _quality_score(cur_items, total)
    cand_score = _quality_score(cand_items, total)
    cur_low = _is_low_conf(cur_items, total)
    cand_low = _is_low_conf(cand_items, total)
    if cand_low and cand_score >= cur_score - 0.02:
        return False
    if cur_low:
        return cand_score + 0.01 < cur_score
    return cand_score + 0.02 < cur_score


def _fmt_items(items, limit: int = 20) -> str:
    out = []
    for i, it in enumerate(items[:limit], start=1):
        out.append(f"{i:02d}. {it.price:6d} | {it.name}")
    if len(items) > limit:
        out.append(f"... and {len(items)-limit} more")
    return "\n".join(out)


def _with_adjustment(items, total: int | None):
    if total is None:
        return items
    s = sum(int(i.price) for i in items)
    diff = int(total) - int(s)
    if diff == 0:
        return items
    from app.storage import Item
    return list(items) + [Item(name="Корректировка по итогу", price=diff)]


def _poor_quality_for_fallback(items, total: int | None, synth_ratio: float) -> bool:
    if total is None:
        return False
    if synth_ratio >= SOFT_FALLBACK_SYNTH_RATIO:
        return True
    if synth_ratio > LOW_CONF_SYNTH_RATIO:
        return True
    if _semantic_fail(items, total):
        return True
    if len(items) <= 2:
        return True
    if len(items) == 1 and "рубли" in (items[0].name or "").lower():
        return True
    return False


def _check_env() -> None:
    if not os.getenv("YANDEX_VISION_API_KEY"):
        raise SystemExit("YANDEX_VISION_API_KEY is not set")
    if not os.getenv("YANDEX_FOLDER_ID"):
        raise SystemExit("YANDEX_FOLDER_ID is not set")


def _resolve_case_variant(path: Path) -> Path | None:
    p = path.expanduser()
    if p.exists():
        return p.resolve()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
        if p.exists():
            return p
    parent = p.parent
    if not parent.exists():
        return None
    target = p.name.lower()
    for cand in parent.iterdir():
        if cand.name.lower() == target:
            return cand.resolve()
    return None


def _resolve_images(custom_images: list[str] | None) -> list[Path]:
    if custom_images:
        paths: list[Path] = []
        for raw in custom_images:
            resolved = _resolve_case_variant(Path(raw))
            if resolved is not None:
                paths.append(resolved)
    else:
        paths = []
        for p in DEFAULT_IMAGES:
            resolved = _resolve_case_variant(p)
            if resolved is not None:
                paths.append(resolved)

    if not paths:
        raise SystemExit("No image files found. Pass one or more --image arguments.")

    # keep order, remove accidental duplicates
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def run(
    images: list[Path],
    report_path: Path,
    save_texts: bool,
    use_llm_fallback: bool,
    save_baseline: bool,
) -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if save_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    if save_texts:
        TEXTS_DIR.mkdir(parents=True, exist_ok=True)

    # Принудительно используем Yandex OCR в этом тесте
    os.environ["OCR_PROVIDER"] = "yandex"

    lines: list[str] = []
    errors = 0
    ratios: list[float] = []
    low_conf_count = 0

    header = (
        f"Yandex OCR integration run at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"SCRIPT_VERSION: {SCRIPT_VERSION}\n"
        f"LLM_FALLBACK_ENABLED: {use_llm_fallback}\n"
        f"OPENAI_KEY_PRESENT: {bool(os.getenv('OPENAI_API_KEY'))}\n"
    )
    lines.append(header)

    for img_path in images:
        started = time.time()
        lines.append(f"\n=== IMAGE: {img_path} ===")
        print(f"[RUN] {img_path.name}")

        try:
            image_bytes = img_path.read_bytes()
            text = extract_text(image_bytes)
            elapsed = time.time() - started

            if save_texts:
                text_out = TEXTS_DIR / f"{img_path.stem}.txt"
                text_out.write_text(text, encoding="utf-8")

            items = parse_receipt_text(text)
            total = extract_total_rub(text)
            items_sum = sum(int(i.price) for i in items)
            diff = (total - items_sum) if total is not None else None
            synthetic = _synthetic_amount(items)
            synth_ratio = (abs(synthetic) / abs(total)) if total else 0.0
            mode_used = "rule"

            if use_llm_fallback and total is not None and _poor_quality_for_fallback(items, total, synth_ratio) and os.getenv("OPENAI_API_KEY"):
                lines.append("LLM_FALLBACK_ATTEMPT: yes")
                try:
                    alt = llm_parse_receipt(text, hint_total_rub=total, hint_items_sum=items_sum)
                    alt_items = alt.items
                    lines.append(f"LLM_PARSE_ITEMS: {len(alt_items)}")
                    if alt_items:
                        alt_items = _with_adjustment(alt_items, total)
                        alt_sum = sum(int(i.price) for i in alt_items)
                        alt_diff = (total - alt_sum) if total is not None else None
                        alt_synthetic = _synthetic_amount(alt_items)
                        alt_synth_ratio = (abs(alt_synthetic) / abs(total)) if total else 0.0
                        if _accept_candidate(items, alt_items, total):
                            items = alt_items
                            items_sum = alt_sum
                            diff = alt_diff
                            synthetic = alt_synthetic
                            synth_ratio = alt_synth_ratio
                            mode_used = "llm_parse"
                    else:
                        # Fallback 2: refine from rule-based items
                        res = llm_refine_receipt_items(text, items, total)
                        if isinstance(res, tuple):
                            refined_items, _ = res
                        elif isinstance(res, LLMResult):
                            refined_items = res.items
                        else:
                            refined_items = res.items
                        lines.append(f"LLM_REFINE_ITEMS: {len(refined_items)}")
                        if refined_items:
                            refined_items = _with_adjustment(refined_items, total)
                            ref_sum = sum(int(i.price) for i in refined_items)
                            ref_diff = (total - ref_sum) if total is not None else None
                            ref_synthetic = _synthetic_amount(refined_items)
                            ref_synth_ratio = (abs(ref_synthetic) / abs(total)) if total else 0.0
                            if _accept_candidate(items, refined_items, total):
                                items = refined_items
                                items_sum = ref_sum
                                diff = ref_diff
                                synthetic = ref_synthetic
                                synth_ratio = ref_synth_ratio
                                mode_used = "llm_refine"
                except Exception as e:
                    lines.append(f"LLM_FALLBACK_ERROR: {type(e).__name__}: {e}")
            else:
                lines.append("LLM_FALLBACK_ATTEMPT: no")

            lines.append(f"OCR time: {elapsed:.2f}s")
            lines.append(f"MODE_USED: {mode_used}")
            lines.append(f"TOTAL   : {total}")
            lines.append(f"SUM     : {items_sum}")
            lines.append(f"DIFF    : {diff}")
            lines.append(f"ITEMS   : {len(items)}")
            lines.append(f"SYNTH   : {synthetic} ({synth_ratio:.1%})")
            lines.append(f"MAX_ITEM_SHARE: {_max_item_share(items, total):.1%}")
            lines.append(f"MAX_NON_ALCOHOL_SHARE: {_max_non_alcohol_item_share(items, total):.1%}")
            sem_fail = _semantic_fail(items, total)
            low_conf = _is_low_conf(items, total)
            lines.append(f"SEMANTIC_FAIL: {sem_fail}")
            lines.append(f"LOW_CONFIDENCE: {low_conf}")
            lines.append("-- ITEMS --")
            lines.append(_fmt_items(items))
            ratios.append(synth_ratio)
            if low_conf:
                low_conf_count += 1

        except Exception as e:
            errors += 1
            lines.append(f"ERROR: {type(e).__name__}: {e}")
            print(f"[ERR] {img_path.name}: {e}")

    if ratios:
        le10 = sum(1 for r in ratios if r <= 0.10)
        gt25 = sum(1 for r in ratios if r > LOW_CONF_SYNTH_RATIO)
        lines.append("\n=== QUALITY SUMMARY ===")
        lines.append(f"IMAGES_EVALUATED: {len(ratios)}")
        lines.append(f"SYNTH_LE_10_PERCENT: {le10}")
        lines.append(f"SYNTH_GT_25_PERCENT: {gt25}")
        lines.append(f"LOW_CONFIDENCE_COUNT: {low_conf_count}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if save_baseline:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        baseline_path = BASELINE_DIR / f"yandex_ocr_report_{stamp}.txt"
        baseline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Baseline saved: {baseline_path}")

    print(f"\nReport saved: {report_path}")
    if save_texts:
        print(f"OCR texts dir: {TEXTS_DIR}")

    if errors:
        print(f"Done with errors: {errors}")
        return 1

    print("Done successfully")
    return 0


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Run Yandex OCR integration test on your own receipt images")
    parser.add_argument(
        "--image",
        action="append",
        default=None,
        help="Image path (can pass multiple times). Required in the public repository.",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help=f"Output report path (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--no-save-texts",
        action="store_true",
        help="Do not save raw OCR texts per image",
    )
    parser.add_argument(
        "--use-llm-fallback",
        action="store_true",
        help="Force-enable llm_parse fallback (also auto-enabled if OPENAI_API_KEY is set).",
    )
    parser.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="Disable llm_parse fallback even if OPENAI_API_KEY is set.",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Also save a timestamped baseline report into logs/baselines.",
    )
    args = parser.parse_args()

    _check_env()
    images = _resolve_images(args.image)
    env_has_openai = bool(os.getenv("OPENAI_API_KEY"))
    use_llm_fallback = (args.use_llm_fallback or env_has_openai) and (not args.no_llm_fallback)

    rc = run(
        images=images,
        report_path=Path(args.report).expanduser().resolve(),
        save_texts=not args.no_save_texts,
        use_llm_fallback=use_llm_fallback,
        save_baseline=args.save_baseline,
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
