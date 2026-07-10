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

    def test_phi_check_allows_clinical_dates_without_other_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Операция выполнена 15.01.2025. Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОБП"],
                clinical_context="контроль после операции 15.01.2025",
            )
            store.accept_case(record.metadata.case_id)

            reference_path = store.promote_to_reference(record.metadata.case_id)
            self.assertTrue(reference_path.exists())

    def test_reference_uses_last_conclusion_when_final_protocol_contains_full_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание исследования.",
                assistant_draft="Черновик заключения.",
                area=["ОБП"],
            )
            final_protocol = """Синтетическое описание исследования.

Заключение:
Старый ошибочный блок внутри полного протокола.

Заключение:
Финальное короткое заключение Романа.

Рекомендации:
Консультация профильного специалиста.
"""
            store.correct_case(record.metadata.case_id, final_protocol)
            reference_path = store.promote_to_reference(record.metadata.case_id)
            reference_text = reference_path.read_text(encoding="utf-8")

            self.assertIn("Заключение:\nФинальное короткое заключение Романа.", reference_text)
            self.assertIn("Рекомендации:\nКонсультация профильного специалиста.", reference_text)
            self.assertNotIn("Старый ошибочный блок", reference_text)

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

    def test_reference_lifecycle_can_deprecate_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)
            reference_path = store.promote_to_reference(record.metadata.case_id, quality="high")
            reference_text = reference_path.read_text(encoding="utf-8")
            self.assertIn("reference_status: active", reference_text)
            self.assertIn("quality: high", reference_text)

            updated_path = store.update_reference_lifecycle(
                record.metadata.case_id,
                reference_status="deprecated",
                quality="low",
                style_version="legacy",
            )
            updated_text = updated_path.read_text(encoding="utf-8")
            self.assertIn("reference_status: deprecated", updated_text)
            self.assertIn("статус: false", updated_text)
            self.assertIn("quality: low", updated_text)
            self.assertIn("style_version: legacy", updated_text)
            active = store.list_references(include_inactive=False)
            self.assertEqual(active, [])


if __name__ == "__main__":
    unittest.main()
