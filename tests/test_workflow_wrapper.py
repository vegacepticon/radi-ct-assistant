import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import radi_ct_workflow


class WorkflowWrapperTest(unittest.TestCase):
    def test_parse_message_with_metadata_and_inline_assistant_draft(self):
        text = """РКТ заключение
Область: ОГК, ОБП
Контекст: синтетический обезличенный пример
Сравнение: да
Режим: fast
---
Описание: синтетический очаг S8 правого легкого уменьшился.

Черновик ассистента:
Уменьшение очага S8 правого легкого.
"""
        message = radi_ct_workflow.parse_workflow_message(text)

        self.assertEqual(message.task, "conclusion")
        self.assertEqual(message.area, ["ОГК", "ОБП"])
        self.assertEqual(message.clinical_context, "синтетический обезличенный пример")
        self.assertTrue(message.comparison)
        self.assertEqual(message.mode, "fast")
        self.assertEqual(message.input_text, "Описание: синтетический очаг S8 правого легкого уменьшился.")
        self.assertEqual(message.assistant_draft, "Уменьшение очага S8 правого легкого.")

    def test_parse_description_and_conclusion_trigger_before_shorter_description_trigger(self):
        message = radi_ct_workflow.parse_workflow_message(
            """РКТ описание + заключение
Область: ОГК
---
Синтетические черновые находки.
"""
        )
        self.assertEqual(message.task, "description_and_conclusion")
        self.assertEqual(message.area, ["ОГК"])
        self.assertEqual(message.input_text, "Синтетические черновые находки.")

    def test_message_command_posts_draft_payload_and_prints_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            message_path = Path(tmp) / "message.md"
            message_path.write_text(
                """РКТ заключение
Область: ОГК
---
Описание: синтетическое описание.
""",
                encoding="utf-8",
            )
            captured = {}

            def fake_request_json(method, path, payload=None, query=None):
                captured["method"] = method
                captured["path"] = path
                captured["payload"] = payload
                return {
                    "case_id": "case-1",
                    "draft": "Синтетическое заключение.",
                    "references_used": [],
                    "path": "/tmp/case-1.md",
                }

            with patch("scripts.radi_ct_workflow.radi_ct_api.request_json", fake_request_json), patch(
                "sys.stdout", new=io.StringIO()
            ) as stdout:
                radi_ct_workflow.main(["message", str(message_path)])

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/draft")
        self.assertEqual(captured["payload"]["task"], "conclusion")
        self.assertEqual(captured["payload"]["area"], ["ОГК"])
        self.assertEqual(captured["payload"]["input_text"], "Описание: синтетическое описание.")
        self.assertIn("RadiCT draft создан", stdout.getvalue())
        self.assertIn("`case-1`", stdout.getvalue())

    def test_message_command_json_mode_prints_raw_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            message_path = Path(tmp) / "message.md"
            message_path.write_text("РКТ заключение\n---\nОписание: синтетическое описание.", encoding="utf-8")

            def fake_request_json(method, path, payload=None, query=None):
                return {"case_id": "case-json", "draft": "Текст", "path": "/tmp/case-json.md"}

            with patch("scripts.radi_ct_workflow.radi_ct_api.request_json", fake_request_json), patch(
                "sys.stdout", new=io.StringIO()
            ) as stdout:
                radi_ct_workflow.main(["--json", "message", str(message_path)])

        data = json.loads(stdout.getvalue())
        self.assertEqual(data["case_id"], "case-json")

    def test_empty_message_returns_value_error(self):
        with self.assertRaises(ValueError):
            radi_ct_workflow.parse_workflow_message("   \n")


if __name__ == "__main__":
    unittest.main()
