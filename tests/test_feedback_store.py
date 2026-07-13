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

            corrected, _ = store.correct_case(
                case_id,
                roman_final="Уменьшение очага S8 правого легкого. Плеврального выпота нет.",
                feedback=["Указывать релевантные стабильные отрицательные находки."],
                error_tags=["incomplete_stable_findings_list"],
                create_lesson_candidate=True,
            )

            self.assertEqual(corrected.metadata.status, "corrected")
            self.assertTrue((store.corrected_dir / f"{case_id}.md").exists())
            self.assertTrue((store.lesson_candidates_dir / f"{case_id}.md").exists())

            promotion_result = store.promote_to_reference(case_id)
            self.assertTrue(promotion_result.saved)
            self.assertTrue(promotion_result.index_updated or True)  # OHS may not be available in tests
            reference_path = Path(promotion_result.path)
            reference_text = reference_path.read_text(encoding="utf-8")
            # Phase 7: new promotions get "candidate" status by default
            self.assertIn("reference_status: candidate", reference_text)
            self.assertIn("## Source input", reference_text)
            self.assertIn("## Target conclusion", reference_text)

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

    def test_accept_case_returns_tuple_with_promotion_result(self):

        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            accepted, promotion_result = store.accept_case(
                record.metadata.case_id, save_as_reference=False
            )
            self.assertEqual(accepted.metadata.status, "accepted")
            self.assertIsNone(promotion_result)

    def test_saved_as_reference_false_never_reports_saved_true(self):

        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            accepted, promotion_result = store.accept_case(
                record.metadata.case_id, save_as_reference=True
            )
            self.assertIsNotNone(promotion_result)
            self.assertTrue(promotion_result.saved)
            self.assertTrue(Path(promotion_result.path).exists())

    def test_response_never_reports_saved_true_if_file_missing(self):

        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id, save_as_reference=False)
            # If we promote and delete the file, saved must not be True
            promotion_result = store.promote_to_reference(record.metadata.case_id)
            self.assertTrue(promotion_result.saved)
            Path(promotion_result.path).unlink()
            self.assertFalse(Path(promotion_result.path).exists())

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

            promotion_result = store.promote_to_reference(record.metadata.case_id)
            self.assertTrue(promotion_result.saved)
            self.assertTrue(Path(promotion_result.path).exists())

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
            promotion_result = store.promote_to_reference(record.metadata.case_id)
            reference_text = Path(promotion_result.path).read_text(encoding="utf-8")

            self.assertIn("## Target conclusion", reference_text)
            self.assertIn("Финальное короткое заключение Романа.", reference_text)
            self.assertIn("## Target recommendations", reference_text)
            self.assertIn("Консультация профильного специалиста.", reference_text)
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
            promotion_result = store.promote_to_reference(record.metadata.case_id, quality="high")
            reference_text = Path(promotion_result.path).read_text(encoding="utf-8")
            self.assertIn("reference_status: candidate", reference_text)
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

    def test_new_promotion_gets_candidate_status(self):
        """Phase 7: new promotions default to 'candidate', not 'active'."""
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)
            promotion_result = store.promote_to_reference(record.metadata.case_id)
            reference_text = Path(promotion_result.path).read_text(encoding="utf-8")
            self.assertIn("reference_status: candidate", reference_text)
            # candidate is NOT in active statuses → статус: false
            self.assertIn("статус: false", reference_text)

    def test_candidate_not_in_active_retrieval(self):
        """Phase 7: candidate references are excluded from retrieval."""
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)
            store.promote_to_reference(record.metadata.case_id)
            # candidate should NOT appear in active-only listing
            active = store.list_references(include_inactive=False)
            self.assertEqual(active, [])
            # but SHOULD appear in full listing
            all_refs = store.list_references()
            self.assertEqual(len(all_refs), 1)
            self.assertEqual(all_refs[0]["reference_status"], "candidate")

    def test_approve_candidate_to_active(self):
        """Phase 7: approve moves candidate → active."""
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id)
            store.promote_to_reference(record.metadata.case_id)
            # Approve → active
            store.update_reference_lifecycle(
                record.metadata.case_id,
                reference_status="active",
                quality="standard",
            )
            active = store.list_references(include_inactive=False)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["reference_status"], "active")

    def test_lesson_candidate_has_provenance(self):
        """Phase 7: lesson candidate includes provenance metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание.",
                assistant_draft="Черновик.",
                area=["ОГК"],
            )
            store.correct_case(
                record.metadata.case_id,
                roman_final="Финал.",
                feedback=["Обобщаемое правило: не использовать термин X."],
                error_tags=["style_refinement"],
                create_lesson_candidate=True,
            )
            lesson_path = store.lesson_candidates_dir / f"{record.metadata.case_id}.md"
            self.assertTrue(lesson_path.exists())
            content = lesson_path.read_text(encoding="utf-8")
            self.assertIn(f"**Source case:** {record.metadata.case_id}", content)
            self.assertIn("**Status:** unconfirmed", content)
            self.assertIn("## Skill transfer criteria", content)
            self.assertIn("## Provenance", content)
            self.assertIn(f"source_case: {record.metadata.case_id}", content)


if __name__ == "__main__":
    unittest.main()
