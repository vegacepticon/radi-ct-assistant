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
            corrected = store.correct_case(
                record.metadata.case_id,
                roman_final="Уменьшение очага S8 правого легкого. Плеврального выпота нет.",
                feedback=["Указывать плевру, если она релевантно отрицательная."],
            )

            reference_path = store.promote_to_reference(corrected.metadata.case_id)
            legacy_path = store.references_dir / f"{corrected.metadata.case_id}.md"

            self.assertEqual(reference_path.parent, store.reference_vault_dir)
            self.assertTrue(reference_path.exists())
            self.assertTrue(legacy_path.exists())
            text = reference_path.read_text(encoding="utf-8")
            self.assertIn("статус: true", text)
            self.assertIn("задача: conclusion", text)
            self.assertIn("Описание:", text)
            self.assertIn("Заключение:", text)

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
            self.assertEqual(results[0].similarity, 0.91)

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


if __name__ == "__main__":
    unittest.main()
