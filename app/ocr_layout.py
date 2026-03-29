from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from io import BytesIO

import requests
from PIL import Image

from app import ocr as ocr_mod


@dataclass(frozen=True)
class OCRLayoutLine:
    text: str
    x: int
    y: int


@dataclass(frozen=True)
class OCRLayoutResult:
    text: str
    lines: list[OCRLayoutLine]


def _line_xy(line_obj: dict) -> tuple[int, int]:
    box = line_obj.get("boundingBox") or {}
    verts = box.get("vertices") or []
    xs: list[int] = []
    ys: list[int] = []
    for v in verts:
        try:
            xs.append(int(v.get("x", 0)))
            ys.append(int(v.get("y", 0)))
        except Exception:
            continue
    if not xs or not ys:
        return (10**9, 10**9)
    return (min(xs), min(ys))


def extract_layout_result(image_bytes: bytes) -> OCRLayoutResult:
    api_key = os.getenv("YANDEX_VISION_API_KEY")
    folder_id = os.getenv("YANDEX_FOLDER_ID")
    if not api_key or not folder_id:
        raise RuntimeError("YANDEX_VISION_API_KEY or YANDEX_FOLDER_ID not set")

    src = Image.open(BytesIO(image_bytes))
    img = ocr_mod._preprocess(src, profile="default")

    def _payload(max_side: int, quality: int) -> dict:
        im = img
        w, h = im.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            im = im.resize((int(w * scale), int(h * scale)))
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        content = base64.b64encode(buf.getvalue()).decode()
        return {
            "folderId": folder_id,
            "analyzeSpecs": [
                {
                    "content": content,
                    "features": [
                        {
                            "type": "TEXT_DETECTION",
                            "textDetectionConfig": {"languageCodes": ["ru", "en"]},
                        }
                    ],
                }
            ],
        }

    url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
    headers = {"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"}
    attempts = [(1500, 65), (1300, 60), (1100, 55)]

    last_body = None
    for idx, (max_side, quality) in enumerate(attempts, start=1):
        r = requests.post(url, json=_payload(max_side, quality), headers=headers, timeout=60)
        if r.status_code == 200:
            data = r.json()
            out: list[OCRLayoutLine] = []
            for res in data.get("results", []):
                for rr in res.get("results", []):
                    td = rr.get("textDetection", {})
                    for page in td.get("pages", []):
                        for block in page.get("blocks", []):
                            for line in block.get("lines", []):
                                words = [w.get("text", "") for w in line.get("words", [])]
                                txt = " ".join(x for x in words if x).strip()
                                if not txt:
                                    continue
                                x, y = _line_xy(line)
                                out.append(OCRLayoutLine(text=txt, x=x, y=y))
            out.sort(key=lambda l: (l.y, l.x))
            text = "\n".join(l.text for l in out)
            return OCRLayoutResult(text=text, lines=out)

        try:
            last_body = r.text
        except Exception:
            last_body = None

        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(1 + idx)
            continue
        r.raise_for_status()

    raise RuntimeError(f"Yandex layout OCR failed. Last body: {last_body}")

