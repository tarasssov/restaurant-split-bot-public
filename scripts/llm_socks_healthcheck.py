from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from openai import OpenAI
import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm import llm_parse_receipt


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description="Simple LLM script-mode healthcheck (supports SOCKS via env)")
    ap.add_argument("--model", default=None, help="Override OPENAI_MODEL")
    ap.add_argument("--require-parse-items", action="store_true", help="Also fail if parse returns zero items")
    args = ap.parse_args()

    _load_dotenv(ROOT / ".env")
    if args.model:
        os.environ["OPENAI_MODEL"] = str(args.model)

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set")

    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    proxy_url = (os.getenv("OPENAI_PROXY_URL") or os.getenv("OPENAI_ALL_PROXY") or "").strip()
    if proxy_url:
        client = OpenAI(http_client=httpx.Client(proxy=proxy_url, timeout=60.0))
    else:
        client = OpenAI()
    ping = client.responses.create(
        model=model,
        input=[{"role": "user", "content": "Reply exactly with: OK"}],
    )
    ping_text = (ping.output_text or "").strip()
    if not ping_text:
        raise SystemExit("LLM healthcheck failed: empty ping response")

    sample = "САЛАТ 450\nКОФЕ 200\nИТОГО 650\n"
    res = llm_parse_receipt(sample, hint_total_rub=650, hint_items_sum=650)
    if args.require_parse_items and len(res.items) < 1:
        raise SystemExit(f"LLM healthcheck failed: parse returned zero items; ping={ping_text!r}")
    print(
        f"LLM_SOCKS_HEALTHCHECK_OK ping={ping_text!r} parse_items={len(res.items)} total={res.total_rub}"
    )


if __name__ == "__main__":
    main()
