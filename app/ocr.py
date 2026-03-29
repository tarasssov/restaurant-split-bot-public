from __future__ import annotations

import os
import time
import base64
import re
from io import BytesIO

import requests
from PIL import Image, ImageOps
import pytesseract

try:
    import pillow_heif
except Exception:
    pillow_heif = None
else:
    # HEIF support is optional: keep OCR startup working on hosts without libheif.
    try:
        pillow_heif.register_heif_opener()
    except Exception:
        pass


# =========================
# Image preprocessing
# =========================
def _preprocess(img: Image.Image, profile: str = "default") -> Image.Image:
    """
    Делаем изображение более OCR-friendly:
    - учитываем EXIF поворот
    - в градации серого
    - автоконтраст
    - лёгкое увеличение (помогает мелкому тексту)
    """
    img = ImageOps.exif_transpose(img)
    if profile == "high_contrast":
        img = img.convert("L")
        img = ImageOps.autocontrast(img, cutoff=2)
        # Лёгкая бинаризация часто помогает на блеклых ценниках.
        img = img.point(lambda p: 255 if p > 165 else 0)
        scale = 1.45
    else:
        img = img.convert("L")
        img = ImageOps.autocontrast(img)
        scale = 1.30

    w, h = img.size
    # Небольшое увеличение помогает мелкому тексту,
    # но потом всё равно будем ужимать до max_side.
    img = img.resize((int(w * scale), int(h * scale)))
    return img


# =========================
# Yandex Vision OCR (Api-Key)
# =========================
def _yandex_vision_ocr(image_bytes: bytes) -> str:
    """
    Авторизация: Api-Key
    .env должен содержать:
      OCR_PROVIDER=yandex
      YANDEX_FOLDER_ID=your_folder_id
      YANDEX_VISION_API_KEY=your_api_key
    """
    api_key = os.getenv("YANDEX_VISION_API_KEY")
    folder_id = os.getenv("YANDEX_FOLDER_ID")

    if not api_key or not folder_id:
        raise RuntimeError("YANDEX_VISION_API_KEY or YANDEX_FOLDER_ID not set in .env")

    url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
    }

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
        return (min(ys), min(xs))

    def _extract_text(data: dict) -> str:
        indexed: list[tuple[int, int, int, str]] = []
        page_idx = 0
        for r in data.get("results", []):
            for rr in r.get("results", []):
                td = rr.get("textDetection", {})
                for page in td.get("pages", []):
                    for block in page.get("blocks", []):
                        for line in block.get("lines", []):
                            words = [w.get("text", "") for w in line.get("words", [])]
                            s = " ".join([x for x in words if x]).strip()
                            if not s:
                                continue
                            y, x = _line_xy(line)
                            indexed.append((page_idx, y, x, s))
                    page_idx += 1
        indexed.sort(key=lambda t: (t[0], t[1], t[2]))

        out: list[str] = []
        prev = None
        for _, _, _, s in indexed:
            # подавляем подряд идущие дубли OCR-блоков
            if s == prev:
                continue
            out.append(s)
            prev = s
        return "\n".join(out)

    def _quality_score(text: str) -> int:
        if not text:
            return 0
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        money_like = len(re.findall(r"\b\d{2,5}(?:[\,\.]\d{1,2})?\b", text))
        priced = len(re.findall(r"\b\d{2,5}[\,\.]\d{2}\b", text))
        alpha_lines = sum(1 for ln in lines if re.search(r"[A-Za-zА-Яа-яЁё]", ln))
        return priced * 8 + money_like * 2 + alpha_lines

    def _make_payload(src_img: Image.Image, max_side: int, quality: int) -> dict:
        im = src_img
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

    # Профили “ужатия”: при 429/5xx уменьшаем дальше.
    attempts = [
        (1400, 60),
        (1200, 55),
        (1100, 55),
        (1000, 50),
        (900, 45),
    ]

    def _run_with_profile(src_img: Image.Image) -> tuple[str, str | None]:
        last_body = None
        for idx, (max_side, quality) in enumerate(attempts, start=1):
            payload = _make_payload(src_img, max_side=max_side, quality=quality)
            resp = requests.post(url, json=payload, headers=headers, timeout=60)

            if resp.status_code == 200:
                data = resp.json()
                return _extract_text(data), None

            try:
                last_body = resp.text
            except Exception:
                last_body = None

            # 429: троттлинг или слишком тяжёлый запрос — ужимаем и повторяем
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    time.sleep(int(ra))
                else:
                    time.sleep(1 + idx)
                continue

            # 5xx: временные ошибки — попробуем ещё (с backoff)
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(1 + idx)
                continue

            # прочие ошибки — сразу наверх
            resp.raise_for_status()
        return "", last_body

    raw_img = Image.open(BytesIO(image_bytes))
    profiles = ["default", "high_contrast"]
    best_text = ""
    best_score = -1
    last_body = None
    for p in profiles:
        text, body = _run_with_profile(_preprocess(raw_img.copy(), profile=p))
        if body:
            last_body = body
        score = _quality_score(text)
        if score > best_score:
            best_score = score
            best_text = text
        # Если текст уже насыщен ценами/строками, не делаем лишние запросы.
        if score >= 220:
            break

    if best_text.strip():
        return best_text
    raise RuntimeError(f"Yandex Vision keeps returning 429/5xx. Last body: {last_body}")


# =========================
# Tesseract fallback
# =========================
def _tesseract_ocr(image_bytes: bytes) -> str:
    img = Image.open(BytesIO(image_bytes))
    img = _preprocess(img, profile="default")
    return pytesseract.image_to_string(img, lang="rus+eng")


# =========================
# Public API
# =========================
def _has_yandex_creds() -> bool:
    return bool(os.getenv("YANDEX_VISION_API_KEY")) and bool(os.getenv("YANDEX_FOLDER_ID"))


def _resolve_provider() -> str:
    provider = (os.getenv("OCR_PROVIDER") or "").strip().lower()
    if provider in {"yandex", "tesseract"}:
        return provider

    # auto/default: предпочитаем Яндекс, если ключи настроены
    if _has_yandex_creds():
        return "yandex"
    return "tesseract"


def extract_text(image_bytes: bytes) -> str:
    provider = _resolve_provider()
    if provider == "yandex":
        return _yandex_vision_ocr(image_bytes)
    return _tesseract_ocr(image_bytes)
