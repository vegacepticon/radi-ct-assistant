import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.radi_ct_workflow import parse_workflow_message
from src.case_schema import is_clarification_response
from src.feedback_store import FeedbackStore
from src.main import app
from src.ohs import ObsidianHybridRetriever
from src.parser import parse_file
from src.prompt_builder import build_prompt


class FindingDescriptionTest(unittest.TestCase):
    def test_clarification_response_detection(self):
        self.assertTrue(is_clarification_response("Уточняющие вопросы:\n1. Какой размер?"))
        self.assertTrue(is_clarification_response("## Уточняющие вопросы:   \n- Где очаг?"))
        self.assertTrue(is_clarification_response("уточняющие вопросы:\nЕсть ли динамика?"))
        self.assertFalse(is_clarification_response("Уточняющие вопросы"))
        self.assertFalse(is_clarification_response("В S8 правого легкого определяется очаг."))

    def test_workflow_trigger_selects_finding_only_mode(self):
        message = parse_workflow_message(
            "ркт описание находки\nОбласть: ОГК\n---\nОчаг в S8 правого легкого 15 мм."
        )
        self.assertEqual(message.task, "finding_description")
        self.assertEqual(message.output_mode, "findings_only")
        self.assertEqual(message.area, ["ОГК"])

    def test_prompt_is_limited_to_one_finding_and_supports_questions(self):
        reference = SimpleNamespace(
            description="очаг справа 12 мм",
            conclusion="В S8 правого легкого определяется очаг размером 12 мм.",
            recommendation="",
            filepath="",
        )
        prompt = build_prompt(
            "очаг справа 15 мм",
            [reference],
            task="finding_description",
            areas=["ОГК"],
            output_mode="full_systematic",  # task contract must still win
        )
        self.assertIn("Формулируй только указанную находку", prompt)
        self.assertIn("Уточняющие вопросы:", prompt)
        self.assertIn("Исходный текст:\nочаг справа 12 мм", prompt)
        self.assertIn("Описание:\nВ S8 правого легкого", prompt)
        self.assertNotIn("Сформируй полное систематическое описание", prompt)
        self.assertNotIn("Заключение:\nВ S8 правого легкого", prompt)

    def test_v2_finding_reference_is_parsed_for_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finding.md"
            path.write_text(
                """---
schema_version: 2
task: finding_description
areas:
  - ОГК
статус: true
reference_status: active
quality: standard
---

## Source input

очаг справа 15 мм

## Target description

В S8 правого легкого определяется солидный очаг размером 15 мм.
""",
                encoding="utf-8",
            )
            entry = parse_file(path)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.area, "ОГК")
            self.assertEqual(entry.metadata["task"], "finding_description")
            self.assertIn("очаг справа", entry.description)
            self.assertIn("солидный очаг", entry.conclusion)

    def test_ohs_finding_retrieval_queries_v2_and_legacy_task_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            reference_path = vault / "finding.md"
            reference_path.write_text(
                """---
schema_version: 2
task: finding_description
areas: [ОГК]
статус: true
reference_status: active
quality: standard
---

## Source input

очаг справа 15 мм

## Target description

В S8 правого легкого определяется солидный очаг размером 15 мм.
""",
                encoding="utf-8",
            )
            fake_output = '[{"path":"finding.md","title":"finding","score":0.9}]'
            with patch("src.ohs.run_ohs", return_value=fake_output) as mocked:
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "очаг справа", area="ОГК", task="finding_description", top_k=3
                )

            # Один и тот же filepath из двух schema-aware запросов дедуплицируется.
            self.assertEqual(len(results), 1)
            self.assertEqual(mocked.call_count, 2)
            calls = [call.args[0] for call in mocked.call_args_list]
            self.assertTrue(any("task:finding_description" in args for args in calls))
            self.assertTrue(any("задача:описание_находки" in args for args in calls))

    def test_ohs_existing_task_keeps_single_broad_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            with patch("src.ohs.run_ohs", return_value="[]") as mocked:
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "описание", task="conclusion", top_k=3
                )
            self.assertEqual(results, [])
            self.assertEqual(mocked.call_count, 1)
            self.assertNotIn("task:conclusion", mocked.call_args.args[0])

    def test_api_rejects_clarification_block_as_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.post(
                "/api/draft",
                json={
                    "input_text": "очаг справа",
                    "task": "finding_description",
                    "assistant_draft": "Уточняющие вопросы:\n1. В каком сегменте расположен очаг?",
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("не является draft", response.text)

            save_response = client.post(
                "/api/save-draft",
                json={
                    "prepared": {
                        "normalized": {
                            "input_text": "очаг справа",
                            "task": "finding_description",
                        }
                    },
                    "assistant_draft": "## Уточняющие вопросы:\n- Какой размер?",
                },
            )
            self.assertEqual(save_response.status_code, 409)

    def test_full_learning_loop_creates_isolated_finding_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            client = TestClient(app)

            draft = client.post(
                "/api/draft",
                json={
                    "input_text": "в S8 справа солидный очаг 15 мм с ровными контурами",
                    "task": "finding_description",
                    "assistant_draft": (
                        "В S8 правого легкого определяется солидный очаг "
                        "размером 15 мм, с ровными четкими контурами."
                    ),
                    "area": ["ОГК"],
                },
            )
            self.assertEqual(draft.status_code, 200)
            case_id = draft.json()["case_id"]

            accepted = client.post(
                f"/api/accept/{case_id}",
                json={"save_as_reference": True},
            )
            self.assertEqual(accepted.status_code, 200)
            outcome = accepted.json()["reference"]
            self.assertTrue(outcome["saved"])

            text = Path(outcome["path"]).read_text(encoding="utf-8")
            self.assertIn("task: finding_description", text)
            self.assertIn("## Source input", text)
            self.assertIn("## Target description", text)
            self.assertNotIn("## Target conclusion", text)

            parsed = parse_file(outcome["path"])
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.metadata["task"], "finding_description")

    def test_promotion_rejects_clarification_text_defensively(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="очаг справа",
                assistant_draft="Уточняющие вопросы:\n1. Какой размер?",
                task="finding_description",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id, save_as_reference=False)
            with self.assertRaisesRegex(ValueError, "Clarifying questions"):
                store.promote_to_reference(record.metadata.case_id)


if __name__ == "__main__":
    unittest.main()
