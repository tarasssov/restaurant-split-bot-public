#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAYS="${1:-7}"
OUT_DIR="$ROOT/logs/quality_reports"
TS="$(date -u +%Y%m%d_%H%M%S)"
OUT_FILE="$OUT_DIR/quality_weekly_${TS}.txt"
LATEST_FILE="$OUT_DIR/quality_weekly_latest.txt"

mkdir -p "$OUT_DIR"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

python3 "$ROOT/scripts/quality_report.py" --days "$DAYS" --out "$OUT_FILE"
cp "$OUT_FILE" "$LATEST_FILE"
python3 "$ROOT/scripts/quality_alert_check.py" >> "$OUT_DIR/weekly_alert.log" 2>&1

echo "Weekly quality report generated:"
echo "  $OUT_FILE"
echo "Latest:"
echo "  $LATEST_FILE"
echo "Alert log:"
echo "  $OUT_DIR/weekly_alert.log"
