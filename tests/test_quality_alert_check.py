from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import quality_alert_check as qac


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class QualityAlertCheckTests(unittest.TestCase):
    def test_alert_bot_token_prefers_dedicated_token(self) -> None:
        with patch.dict(
            os.environ,
            {"BOT_TOKEN": "main_token", "QUALITY_ALERT_BOT_TOKEN": "alert_token"},
            clear=False,
        ):
            self.assertEqual(qac._resolve_alert_bot_token(), "alert_token")

    def test_alert_bot_token_falls_back_to_main_token(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "main_token"}, clear=False):
            os.environ.pop("QUALITY_ALERT_BOT_TOKEN", None)
            self.assertEqual(qac._resolve_alert_bot_token(), "main_token")

    def test_alert_project_name_from_env(self) -> None:
        with patch.dict(os.environ, {"QUALITY_ALERT_PROJECT_NAME": "mega-pro-bot"}, clear=False):
            self.assertEqual(qac._resolve_project_name(), "mega-pro-bot")

    def test_sends_only_for_new_problem_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_path = root / "logs" / "receipt_sessions.jsonl"
            state_path = root / "logs" / "quality_reports" / "alert_state.json"
            rows = [
                {
                    "event": "receipt_session",
                    "timestamp": "2026-03-01T10:00:00+00:00",
                    "session_id": "s1",
                    "quality_status": "good",
                    "quality_reasons": [],
                    "metrics": {"synthetic_ratio": 0.0, "diff_rub": 0, "items_count": 10},
                },
                {
                    "event": "receipt_session",
                    "timestamp": "2026-03-02T10:00:00+00:00",
                    "session_id": "s2",
                    "quality_status": "low_confidence",
                    "quality_reasons": ["synthetic_high"],
                    "metrics": {"synthetic_ratio": 0.4, "diff_rub": 0, "items_count": 2},
                },
            ]
            _write_jsonl(sessions_path, rows)

            old_sessions_log = qac.SESSIONS_LOG
            try:
                qac.SESSIONS_LOG = sessions_path
                decision = qac.evaluate(
                    state_path=state_path,
                    min_new_fails=1,
                    force_send=False,
                    tz_name="Europe/Moscow",
                    project_name="project-x",
                )
            finally:
                qac.SESSIONS_LOG = old_sessions_log

            self.assertTrue(decision.send)
            self.assertEqual(decision.new_problem_count, 1)
            self.assertEqual(decision.unsent_problem_count, 1)
            self.assertIn("Project: project-x", decision.message)

    def test_no_duplicate_after_state_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_path = root / "logs" / "receipt_sessions.jsonl"
            state_path = root / "logs" / "quality_reports" / "alert_state.json"
            rows = [
                {
                    "event": "receipt_session",
                    "timestamp": "2026-03-02T10:00:00+00:00",
                    "session_id": "s2",
                    "quality_status": "low_confidence",
                    "quality_reasons": ["synthetic_high"],
                    "metrics": {"synthetic_ratio": 0.4, "diff_rub": 0, "items_count": 2},
                },
            ]
            _write_jsonl(sessions_path, rows)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "last_seen_ts": "2026-03-02T10:00:00+00:00",
                        "last_seen_sid": "s2",
                        "sent_problem_session_ids": ["s2"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            old_sessions_log = qac.SESSIONS_LOG
            try:
                qac.SESSIONS_LOG = sessions_path
                decision = qac.evaluate(
                    state_path=state_path,
                    min_new_fails=1,
                    force_send=False,
                    tz_name="Europe/Moscow",
                )
            finally:
                qac.SESSIONS_LOG = old_sessions_log

            self.assertFalse(decision.send)
            self.assertEqual(decision.new_sessions_count, 0)
            self.assertEqual(decision.unsent_problem_count, 0)


if __name__ == "__main__":
    unittest.main()
