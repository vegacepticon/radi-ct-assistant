import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app


class LearningLoopApiTest(unittest.TestCase):
    def test_draft_requires_assistant_draft_in_hermes_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетический очаг S8 правого легкого уменьшился.",
                    "task": "conclusion",
                },
            )
            self.assertEqual(response.status_code, 422)

            blank_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетический очаг S8 правого легкого уменьшился.",
                    "assistant_draft": "   ",
                    "task": "conclusion",
                },
            )
            self.assertEqual(blank_response.status_code, 400)
            self.assertIn("assistant_draft", blank_response.text)

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
                    "references_used": ["/tmp/reference-1.md"],
                },
            )
            self.assertEqual(draft_response.status_code, 200)
            draft_data = draft_response.json()
            case_id = draft_data["case_id"]
            self.assertEqual(draft_data["draft"], "Уменьшение очага S8 правого легкого.")
            self.assertEqual(draft_data["references_used"], ["/tmp/reference-1.md"])
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

    def test_cases_detail_and_promote_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)

            draft_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетический малый очаг S3 левого легкого без динамики.",
                    "assistant_draft": "Очаг S3 левого легкого без динамики.",
                    "area": ["ОГК"],
                    "clinical_context": "синтетический пример",
                },
            )
            self.assertEqual(draft_response.status_code, 200)
            case_id = draft_response.json()["case_id"]

            correct_response = client.post(
                f"/api/correct/{case_id}",
                json={
                    "roman_final": "Очаг S3 левого легкого без динамики.",
                    "feedback": ["Формулировка приемлема."],
                    "error_tags": [],
                },
            )
            self.assertEqual(correct_response.status_code, 200)

            cases_response = client.get("/api/cases")
            self.assertEqual(cases_response.status_code, 200)
            cases = cases_response.json()
            statuses = {item["status"] for item in cases if item["case_id"] == case_id}
            self.assertIn("draft", statuses)
            self.assertIn("corrected", statuses)

            filtered_response = client.get("/api/cases", params={"status": "corrected"})
            self.assertEqual(filtered_response.status_code, 200)
            self.assertTrue(all(item["status"] == "corrected" for item in filtered_response.json()))

            invalid_filter_response = client.get("/api/cases", params={"status": "archived"})
            self.assertEqual(invalid_filter_response.status_code, 400)

            detail_response = client.get(f"/api/cases/{case_id}")
            self.assertEqual(detail_response.status_code, 200)
            detail = detail_response.json()
            self.assertEqual(detail["case_id"], case_id)
            self.assertEqual(detail["status"], "corrected")
            self.assertEqual(detail["roman_final"], "Очаг S3 левого легкого без динамики.")
            self.assertEqual(detail["feedback"], ["Формулировка приемлема."])

            promote_response = client.post(f"/api/references/promote/{case_id}")
            self.assertEqual(promote_response.status_code, 200)
            reference_path = Path(promote_response.json()["reference_path"])
            self.assertTrue(reference_path.exists())
            reference_text = reference_path.read_text(encoding="utf-8")
            self.assertIn("статус: true", reference_text)
            self.assertIn("Заключение:", reference_text)

    def test_promote_draft_and_phi_guard_return_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)

            draft_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетическое описание без идентификаторов.",
                    "assistant_draft": "Синтетическое заключение.",
                },
            )
            self.assertEqual(draft_response.status_code, 200)
            draft_case_id = draft_response.json()["case_id"]
            draft_promote_response = client.post(f"/api/references/promote/{draft_case_id}")
            self.assertEqual(draft_promote_response.status_code, 400)

            phi_draft_response = client.post(
                "/api/draft",
                json={
                    "input_text": "Описание: синтетическое описание без идентификаторов.",
                    "assistant_draft": "Синтетическое заключение.",
                },
            )
            self.assertEqual(phi_draft_response.status_code, 200)
            phi_case_id = phi_draft_response.json()["case_id"]
            correct_response = client.post(
                f"/api/correct/{phi_case_id}",
                json={"roman_final": "Пациент Иванов Иван Иванович. Синтетическое заключение."},
            )
            self.assertEqual(correct_response.status_code, 200)
            phi_promote_response = client.post(f"/api/references/promote/{phi_case_id}")
            self.assertEqual(phi_promote_response.status_code, 400)
            self.assertIn("Potential PHI", phi_promote_response.text)

    def test_rag_context_endpoint_builds_prompt_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            fake_reference = SimpleNamespace(
                description="Описание похожего синтетического случая.",
                conclusion="Заключение похожего синтетического случая.",
                recommendation="",
                filepath="/tmp/reference-vault/ref-1.md",
                title="ref-1",
                area="ОГК",
                similarity=0.87,
            )

            class FakeRetriever:
                def search(self, query_description, area="", task="conclusion", top_k=5):
                    self.query_description = query_description
                    self.area = area
                    self.task = task
                    self.top_k = top_k
                    return [fake_reference]

            fake_retriever = FakeRetriever()
            with patch("src.main.get_retriever", return_value=fake_retriever):
                response = client.post(
                    "/api/rag/context",
                    json={
                        "input_text": "Описание: синтетический очаг S8 уменьшился.",
                        "task": "conclusion",
                        "area": ["ОГК"],
                        "clinical_context": "синтетический контекст",
                        "top_k": 3,
                    },
                )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["references_used"], ["/tmp/reference-vault/ref-1.md"])
            self.assertIn("--- Пример 1 ---", data["prompt"])
            self.assertIn("синтетический контекст", data["prompt"])
            self.assertEqual(fake_retriever.area, "ОГК")
            self.assertEqual(fake_retriever.task, "conclusion")
            self.assertEqual(fake_retriever.top_k, 3)

    def test_accept_missing_case_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.post("/api/accept/missing-case", json={})
            self.assertEqual(response.status_code, 404)
            detail_response = client.get("/api/cases/missing-case")
            self.assertEqual(detail_response.status_code, 404)

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
