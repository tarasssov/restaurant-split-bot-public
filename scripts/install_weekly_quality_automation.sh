#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_BASE="$ROOT/ops/automation"
CRON_DIR="$OUT_BASE/cron"
LAUNCHD_DIR="$OUT_BASE/launchd"
LOG_DIR="$ROOT/logs/quality_reports"
WEEKDAY_RAW="$(echo "${QUALITY_ALERT_WEEKDAY:-MON}" | tr '[:lower:]' '[:upper:]')"
HOUR="${QUALITY_ALERT_HOUR:-10}"
ALERT_TZ="${QUALITY_ALERT_TZ:-Europe/Moscow}"

case "$WEEKDAY_RAW" in
  MON) CRON_WEEKDAY=1; LAUNCHD_WEEKDAY=2 ;;
  TUE) CRON_WEEKDAY=2; LAUNCHD_WEEKDAY=3 ;;
  WED) CRON_WEEKDAY=3; LAUNCHD_WEEKDAY=4 ;;
  THU) CRON_WEEKDAY=4; LAUNCHD_WEEKDAY=5 ;;
  FRI) CRON_WEEKDAY=5; LAUNCHD_WEEKDAY=6 ;;
  SAT) CRON_WEEKDAY=6; LAUNCHD_WEEKDAY=7 ;;
  SUN) CRON_WEEKDAY=0; LAUNCHD_WEEKDAY=1 ;;
  *)   CRON_WEEKDAY=1; LAUNCHD_WEEKDAY=2 ;;
esac

mkdir -p "$CRON_DIR" "$LAUNCHD_DIR" "$LOG_DIR"

CRON_FILE="$CRON_DIR/weekly_quality_report.cron"
LAUNCHD_LABEL="com.restaurantsplit.weekly-quality-report"
LAUNCHD_FILE="$LAUNCHD_DIR/${LAUNCHD_LABEL}.plist"

cat > "$CRON_FILE" <<EOF
# Weekly quality report + alert
TZ=$ALERT_TZ
0 $HOUR * * $CRON_WEEKDAY cd "$ROOT" && ./scripts/run_weekly_quality_report.sh 7 >> "$LOG_DIR/weekly_runner.log" 2>&1
EOF

cat > "$LAUNCHD_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCHD_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$ROOT" && ./scripts/run_weekly_quality_report.sh 7 >> "$LOG_DIR/weekly_runner.log" 2>&1</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>TZ</key>
    <string>$ALERT_TZ</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>$LAUNCHD_WEEKDAY</integer>
    <key>Hour</key>
    <integer>$HOUR</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/launchd_stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/launchd_stderr.log</string>
</dict>
</plist>
EOF

echo "Automation templates created:"
echo "  cron:    $CRON_FILE"
echo "  launchd: $LAUNCHD_FILE"
echo "Schedule:  weekday=$WEEKDAY_RAW hour=$HOUR:00 tz=$ALERT_TZ"
echo
echo "Install on macOS (launchd):"
echo "  cp \"$LAUNCHD_FILE\" ~/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
echo "  launchctl unload ~/Library/LaunchAgents/$LAUNCHD_LABEL.plist 2>/dev/null || true"
echo "  launchctl load ~/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
echo "  launchctl list | grep $LAUNCHD_LABEL"
echo
echo "Install on Linux (cron):"
echo "  (crontab -l 2>/dev/null; cat \"$CRON_FILE\") | crontab -"
echo "  crontab -l | grep run_weekly_quality_report.sh"
