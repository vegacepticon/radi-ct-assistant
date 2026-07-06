import json
import tempfile
import unittest
from pathlib import Path

from src.feedback_store import FeedbackStore


class FeedbackStoreTest(unittest.TestCase):
    def test_create_correct_lesson_and_promote_synthetic_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="В S8 правого легкого очаг 20 x 13 мм, ранее 23 x 23 мм. Плевра свободна.",
                assistant_draft="Положительная динамика очага S8 правого легкого.",
                area=["ОГК"],
                clinical_context="синтетический онкологический пример",
                comparison=True,
            )

            case_id = record.metadata.case_id
            self.assertTrue((store.drafts_dir / f"{case_id}.md").exists())

            corrected = store.correct_case(
                case_id,
                roman_final="Уменьшение очага S8 правого легкого. Плеврального выпота нет.",
                feedback=["Указывать релевантные стабильные отрицательные находки."],
                error_tags=["incomplete_stable_findings_list"],
                create_lesson_candidate=True,
            )

            self.assertEqual(corrected.metadata.status, "corrected")
            self.assertTrue((store.corrected_dir / f"{case_id}.md").exists())
            self.assertTrue((store.lesson_candidates_dir / f"{case_id}.md").exists())

            reference_path = store.promote_to_reference(case_id)
            reference_text = reference_path.read_text(encoding="utf-8")
            self.assertIn("статус: true", reference_text)
            self.assertIn("Описание:", reference_text)
            self.assertIn("Заключение:", reference_text)

            events = [
                json.loads(line)
                for line in store.feedback_log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(events[0]["case_id"], case_id)
            self.assertEqual(events[0]["error_tags"], ["incomplete_stable_findings_list"])
            self.assertTrue(events[-1]["promoted_to_reference"])

    def test_phi_check_blocks_direct_identifier_on_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание. Номер исследования 1234567.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)

            with self.assertRaises(ValueError):
                store.promote_to_reference(record.metadata.case_id)

    def test_list_cases_keeps_audit_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)

            statuses = {item["status"] for item in store.list_cases()}
            self.assertIn("draft", statuses)
            self.assertIn("accepted", statuses)


if __name__ == "__main__":
    unittest.main()
