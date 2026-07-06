import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.main import app


class LearningLoopApiTest(unittest.TestCase):
    def test_draft_correct_lessons_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)

            draft_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетический очаг S8 правого легкого уменьшился. Плевра свободна.",
                    "assistant_draft": "Уменьшение очага S8 правого легкого.",
                    "task": "conclusion",
                    "input_type": "markdown",
                    "area": ["ОГК"],
                    "clinical_context": "синтетический пример",
                    "comparison": True,
                },
            )
            self.assertEqual(draft_response.status_code, 200)
            draft_data = draft_response.json()
            case_id = draft_data["case_id"]
            self.assertEqual(draft_data["draft"], "Уменьшение очага S8 правого легкого.")
            self.assertTrue(Path(draft_data["path"]).exists())

            correct_response = client.post(
                f"/api/correct/{case_id}",
                json={
                    "roman_final": "Уменьшение очага S8 правого легкого. Плеврального выпота нет.",
                    "feedback": "- Указывать релевантные отрицательные стабильные находки.",
                    "error_tags": ["incomplete_stable_findings_list"],
                    "create_lesson_candidate": True,
                },
            )
            self.assertEqual(correct_response.status_code, 200)
            self.assertEqual(correct_response.json()["status"], "corrected")

            lessons_response = client.get("/api/lessons")
            self.assertEqual(lessons_response.status_code, 200)
            lessons = lessons_response.json()
            self.assertEqual(len(lessons), 1)
            self.assertIn("incomplete_stable_findings_list", lessons[0]["content"])

            log_path = Path(tmp) / "data" / "feedback" / "feedback_log.jsonl"
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["case_id"], case_id)

    def test_accept_missing_case_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.post("/api/accept/missing-case", json={})
            self.assertEqual(response.status_code, 404)

    def test_correct_empty_final_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            draft_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетическое описание.",
                    "assistant_draft": "Синтетическое заключение.",
                },
            )
            case_id = draft_response.json()["case_id"]
            response = client.post(f"/api/correct/{case_id}", json={"roman_final": "   "})
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
