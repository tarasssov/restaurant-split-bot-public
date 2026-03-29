#!/usr/bin/env bash
set -euo pipefail

# --- load .env ---
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
else
  echo "❌ .env file not found in project root"
  exit 1
fi

# --- check env ---
if [ -z "$YANDEX_VISION_API_KEY" ]; then
  echo "❌ YANDEX_VISION_API_KEY is not set"
  exit 1
fi

if [ -z "$YANDEX_FOLDER_ID" ]; then
  echo "❌ YANDEX_FOLDER_ID is not set"
  exit 1
fi

# --- paths ---
OUT_DIR="$PROJECT_ROOT/fixtures/generated"

mkdir -p "$OUT_DIR"

# --- process images ---
if [ "$#" -eq 0 ]; then
  echo "Usage: $0 /abs/path/to/receipt1.jpg [/abs/path/to/receipt2.jpg ...]"
  exit 1
fi

for img in "$@"; do
  IMG_PATH="$img"

  if [ ! -f "$IMG_PATH" ]; then
    echo "⚠️  Image not found: $IMG_PATH"
    continue
  fi

  BASENAME="$(basename "$img")"
  NAME="${BASENAME%.*}"
  OUT_FILE="$OUT_DIR/${NAME}.txt"

  echo "🧠 OCR → $OUT_FILE"

  python3 - <<PY
from pathlib import Path
from app.ocr import extract_text

img_path = Path("$IMG_PATH")
text = extract_text(img_path.read_bytes())

Path("$OUT_FILE").write_text(text, encoding="utf-8")
print("✅ saved:", "$OUT_FILE")
PY

done

echo "🎉 OCR batch completed"
