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
            self.assertIn("статус: true", text)
            self.assertIn("задача: conclusion", text)
            self.assertIn("Описание:", text)
            self.assertIn("Заключение:", text)

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


if __name__ == "__main__":
    unittest.main()
