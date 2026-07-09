import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import radi_ct_workflow


class WorkflowCaptureTest(unittest.TestCase):
    def test_parse_session_capture_message(self):
        text = """РКТ заключение
Область: ОГК, ОБП
Контекст: синтетическая травма
---
Описание синтетического исследования.

Черновик ассистента:
Черновик заключения.

Финальный вариант:
Финальное заключение Романа.

Почему:
- Убраны лишние детали.
- Добавлена вероятностная формулировка.
"""
        message, roman_final, feedback = radi_ct_workflow.parse_session_capture_message(text)

        self.assertEqual(message.task, "conclusion")
        self.assertEqual(message.area, ["ОГК", "ОБП"])
        self.assertEqual(message.clinical_context, "синтетическая травма")
        self.assertEqual(message.input_text, "Описание синтетического исследования.")
        self.assertEqual(message.assistant_draft, "Черновик заключения.")
        self.assertEqual(roman_final, "Финальное заключение Романа.")
        self.assertIn("Убраны лишние детали", feedback)

    def test_capture_session_creates_draft_then_correct_with_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.md"
            session_path.write_text(
                """РКТ заключение
Область: ОГК
Контекст: синтетический обезличенный пример
---
Описание синтетического исследования.

Черновик ассистента:
Черновик заключения.

Финальный вариант:
Финальное заключение Романа.

Почему:
- Финальная формулировка принята как reference.
""",
                encoding="utf-8",
            )
            calls = []

            def fake_request_json(method, path, payload=None, query=None):
                self.assertIsNotNone(payload)
                assert payload is not None
                calls.append({"method": method, "path": path, "payload": payload, "query": query})
                if path == "/api/draft":
                    return {
                        "case_id": "2026-07-09-001",
                        "draft": payload["assistant_draft"],
                        "path": "/tmp/draft.md",
                    }
                if path == "/api/correct/2026-07-09-001":
                    return {
                        "case_id": "2026-07-09-001",
                        "status": "corrected",
                        "path": "/tmp/corrected.md",
                        "saved_as_reference": payload["save_as_reference"],
                    }
                raise AssertionError(path)

            with patch.object(radi_ct_workflow.radi_ct_api, "request_json", fake_request_json):
                with patch("sys.stdout"):
                    radi_ct_workflow.main(["--json", "capture-session", str(session_path), "--tag", "style_refinement"])

            self.assertEqual(calls[0]["path"], "/api/draft")
            self.assertEqual(calls[0]["payload"]["assistant_draft"], "Черновик заключения.")
            self.assertEqual(calls[1]["path"], "/api/correct/2026-07-09-001")
            self.assertEqual(calls[1]["payload"]["roman_final"], "Финальное заключение Романа.")
            self.assertTrue(calls[1]["payload"]["save_as_reference"])
            self.assertEqual(calls[1]["payload"]["error_tags"], ["style_refinement"])


if __name__ == "__main__":
    unittest.main()
