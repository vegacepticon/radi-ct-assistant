import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.feedback_store import FeedbackStore
from src.main import app
from src.ohs import ObsidianHybridRetriever, ohs_status, resolve_ohs_command


class ObsidianHybridRagTest(unittest.TestCase):
    def test_promote_writes_reference_vault_and_legacy_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Описание: синтетический очаг S8 правого легкого уменьшился. Плевра свободна.",
                assistant_draft="Уменьшение очага S8 правого легкого.",
                area=["ОГК"],
                clinical_context="синтетический пример",
                comparison=True,
            )
            corrected, _ = store.correct_case(
                record.metadata.case_id,
                roman_final="Уменьшение очага S8 правого легкого. Плеврального выпота нет.",
                feedback=["Указывать плевру, если она релевантно отрицательная."],
            )

            promotion_result = store.promote_to_reference(corrected.metadata.case_id)
            self.assertTrue(promotion_result.saved)
            reference_path = Path(promotion_result.path)
            legacy_path = store.references_dir / f"{corrected.metadata.case_id}.md"

            self.assertEqual(reference_path.parent, store.reference_vault_dir)
            self.assertTrue(reference_path.exists())
            self.assertTrue(legacy_path.exists())
            text = reference_path.read_text(encoding="utf-8")
            self.assertIn("reference_status: candidate", text)
            self.assertIn("задача: conclusion", text)
            self.assertIn("## Source input", text)
            self.assertIn("## Target conclusion", text)

    def test_promotion_reports_reindex_failure_as_partial_success(self):
        """Reference saved + reindex failure: saved=True, index_updated=False."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_AUTO_REINDEX"] = "1"
            os.environ["RADI_CT_BASE_DIR"] = tmp
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Описание: синтетический очаг S8 правого легкого.",
                assistant_draft="Уменьшение очага S8 правого легкого.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id, save_as_reference=False)

            # Simulate OHS reindex failure
            with patch("src.ohs.ohs_reindex", side_effect=RuntimeError("OHS command not found")):
                promotion_result = store.promote_to_reference(record.metadata.case_id)

            self.assertTrue(promotion_result.saved)
            self.assertTrue(Path(promotion_result.path).exists())
            self.assertFalse(promotion_result.index_updated)
            self.assertIn("OHS command not found", promotion_result.index_error)

    def test_ohs_retriever_parses_search_results_and_reference_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            reference = vault / "case-1.md"
            reference.write_text(
                """---
анамнез: синтетический пример
область:
  - ОГК
сравнение: true
экстренность: false
статус: true
задача: conclusion
---

Описание:
Очаг S8 правого легкого уменьшился. Плевра свободна.

Заключение:
Положительная динамика очага S8 правого легкого. Плеврального выпота нет.
""",
                encoding="utf-8",
            )
            fake_output = json.dumps(
                [
                    {
                        "path": "case-1.md",
                        "title": "case-1",
                        "score": 0.91,
                    }
                ],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "очаг S8 уменьшился", area="ОГК", task="conclusion", top_k=3
                )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].area, "ОГК")
            self.assertIn("Очаг S8", results[0].description)
            self.assertIn("Положительная динамика", results[0].conclusion)
            # Similarity is now a blended score: OHS semantic score plus
            # reference lifecycle priority (quality/status/recency).
            self.assertGreater(results[0].similarity, 0.85)
            self.assertLess(results[0].similarity, 0.87)

    def test_ohs_status_reports_missing_command_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.ohs.resolve_ohs_command", return_value=None):
                status = ohs_status(vault_dir=Path(tmp))

        self.assertFalse(status.available)
        self.assertIn("not found", status.error)

    def test_resolve_ohs_command_accepts_absolute_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "obsidian-hybrid-search"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

            self.assertEqual(resolve_ohs_command(str(executable)), str(executable))

    def test_api_rag_status_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            with patch(
                "src.rag.rag_status",
                return_value={
                    "backend": "obsidian_hybrid",
                    "available": True,
                    "command": "/tmp/obsidian-hybrid-search",
                    "vault": tmp,
                    "total": 1,
                    "indexed": 1,
                    "chunks": 1,
                    "model": "local:test",
                    "version": "test",
                    "error": "",
                },
            ):
                response = client.get("/api/rag/status")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["backend"], "obsidian_hybrid")
            self.assertTrue(data["available"])
            self.assertEqual(data["indexed"], 1)

    def test_reference_vault_and_legacy_mirror_have_identical_content_after_promotion(self):
        """P2: data/reference-vault/ and data/references/ must contain identical files after promotion."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id, save_as_reference=False)
            promotion_result = store.promote_to_reference(record.metadata.case_id)

            vault_path = Path(promotion_result.path)
            legacy_path = store.references_dir / f"{record.metadata.case_id}.md"

            self.assertTrue(vault_path.exists())
            self.assertTrue(legacy_path.exists())
            # Contents must be identical
            self.assertEqual(
                vault_path.read_text(encoding="utf-8"),
                legacy_path.read_text(encoding="utf-8"),
                "reference-vault and legacy references mirror diverged after promotion",
            )

    def test_reference_vault_and_legacy_mirror_after_lifecycle_update(self):
        """P2: mirror equality must hold after lifecycle update."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            store = FeedbackStore(base_dir=Path(tmp))
            record = store.create_case(
                input_text="Синтетическое описание без идентификаторов.",
                assistant_draft="Синтетическое заключение.",
                area=["ОГК"],
            )
            store.accept_case(record.metadata.case_id, save_as_reference=False)
            store.promote_to_reference(record.metadata.case_id)

            # Update lifecycle
            store.update_reference_lifecycle(
                record.metadata.case_id,
                reference_status="deprecated",
                quality="low",
            )

            vault_path = store.reference_vault_dir / f"{record.metadata.case_id}.md"
            legacy_path = store.references_dir / f"{record.metadata.case_id}.md"
            self.assertEqual(
                vault_path.read_text(encoding="utf-8"),
                legacy_path.read_text(encoding="utf-8"),
                "reference-vault and legacy mirror diverged after lifecycle update",
            )

    def test_ohs_retriever_excludes_deprecated_and_prefers_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            deprecated = vault / "old.md"
            deprecated.write_text(
                """---
анамнез: синтетический пример
область:
  - ОГК
сравнение: true
экстренность: false
статус: false
задача: conclusion
reference_status: deprecated
quality: low
---

Описание:
Старое описание.

Заключение:
Старое заключение.
""",
                encoding="utf-8",
            )
            gold = vault / "gold.md"
            gold.write_text(
                """---
анамнез: синтетический пример
область:
  - ОГК
сравнение: true
экстренность: false
статус: true
задача: conclusion
reference_status: gold
quality: gold
created_at: 2026-07-09T12:00:00+03:00
updated_at: 2026-07-09T12:00:00+03:00
---

Описание:
Актуальное описание.

Заключение:
Актуальное заключение.
""",
                encoding="utf-8",
            )
            fake_output = json.dumps(
                [
                    {"path": "old.md", "title": "old", "score": 0.99},
                    {"path": "gold.md", "title": "gold", "score": 0.80},
                ],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "синтетический запрос", area="ОГК", task="conclusion", top_k=3
                )

            self.assertEqual(len(results), 1)
            self.assertIn("Актуальное", results[0].conclusion)

    def test_dedup_by_filepath(self):
        """OHS может вернуть один файл дважды (разные chunks) — берём только первый."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ref = vault / "ref-1.md"
            ref.write_text(
                """---
анамнез: синтетический пример
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Синтетическое описание очага.

Заключение:
Синтетическое заключение.
""",
                encoding="utf-8",
            )
            # Same path returned twice
            fake_output = json.dumps(
                [
                    {"path": "ref-1.md", "title": "ref-1", "score": 0.95},
                    {"path": "ref-1.md", "title": "ref-1", "score": 0.90},
                ],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "синтетический запрос", area="ОГК", task="conclusion", top_k=5
                )

            self.assertEqual(len(results), 1, "Duplicate filepath should be removed")

    def test_no_good_hits_when_score_below_absolute_threshold(self):
        """Если лучший candidate ниже absolute threshold, возвращаем пустой список."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ref = vault / "ref-low.md"
            ref.write_text(
                """---
анамнез: синтетический пример
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Синтетическое описание.

Заключение:
Синтетическое заключение.
""",
                encoding="utf-8",
            )
            # Score below absolute threshold (0.45)
            fake_output = json.dumps(
                [{"path": "ref-low.md", "title": "ref-low", "score": 0.30}],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "нерелевантный запрос", area="ОГК", task="conclusion", top_k=5
                )

            self.assertEqual(len(results), 0, "Low-score candidates should be filtered")

    def test_diversity_removes_near_identical_descriptions(self):
        """Два файла с очень похожим description — оставляем только первый."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ref1 = vault / "ref-a.md"
            ref1.write_text(
                """---
анамнез: синтетический
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Очаг S8 правого легкого 15 мм. Плевра свободна. Лимфоузлы не увеличены.

Заключение:
Очаг S8 правого легкого.
""",
                encoding="utf-8",
            )
            ref2 = vault / "ref-b.md"
            ref2.write_text(
                """---
анамнез: синтетический
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Очаг S8 правого легкого 15 мм. Плевра свободна. Лимфоузлы не увеличены.

Заключение:
Очаг S8 правого легкого без динамики.
""",
                encoding="utf-8",
            )
            # Both with high scores
            fake_output = json.dumps(
                [
                    {"path": "ref-a.md", "title": "ref-a", "score": 0.95},
                    {"path": "ref-b.md", "title": "ref-b", "score": 0.93},
                ],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "очаг S8 правого легкого", area="ОГК", task="conclusion", top_k=5
                )

            self.assertEqual(len(results), 1, "Near-identical descriptions should be deduplicated")

    def test_diversity_keeps_different_descriptions(self):
        """Два файла с разными descriptions — оставляем оба."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ref1 = vault / "ref-x.md"
            ref1.write_text(
                """---
анамнез: синтетический
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Очаг S8 правого легкого 15 мм. Плевра свободна.

Заключение:
Очаг S8 правого легкого.
""",
                encoding="utf-8",
            )
            ref2 = vault / "ref-y.md"
            ref2.write_text(
                """---
анамнез: синтетический
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Пневмония нижней доли правого легкого. Экссудативный плеврит справа.

Заключение:
Пневмония. Плевральный выпот.
""",
                encoding="utf-8",
            )
            fake_output = json.dumps(
                [
                    {"path": "ref-x.md", "title": "ref-x", "score": 0.92},
                    {"path": "ref-y.md", "title": "ref-y", "score": 0.88},
                ],
                ensure_ascii=False,
            )

            with patch("src.ohs.run_ohs", return_value=fake_output):
                results = ObsidianHybridRetriever(vault_dir=vault).search(
                    "патология легких", area="ОГК", task="conclusion", top_k=5
                )

            self.assertEqual(len(results), 2, "Different descriptions should both be kept")


if __name__ == "__main__":
    unittest.main()
