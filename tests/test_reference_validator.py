"""
Тесты для reference_validator.

Используют только synthetic no-PHI fixtures — никаких реальных clinical data.
"""
import os
import tempfile
import unittest
from pathlib import Path

from src.reference_validator import (
    AuditReport,
    ReferenceReport,
    Severity,
    validate_directory,
    validate_reference,
)


def _write_reference(
    path: Path,
    metadata: dict | None = None,
    body: str = "",
) -> None:
    """Быстро записать .md reference с YAML frontmatter."""
    import yaml

    fm = metadata or {}
    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    text = f"---\n{fm_text}\n---\n\n{body}"
    path.write_text(text, encoding="utf-8")


class TestReferenceValidator(unittest.TestCase):
    """Тесты валидатора на synthetic fixtures."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.tmp = Path(self.tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_ref(self, name: str, metadata: dict, body: str) -> Path:
        path = self.tmp / name
        _write_reference(path, metadata, body)
        return path

    # --- Базовые случаи: валидный reference ---

    def test_valid_conclusion_reference(self):
        """Корректный conclusion reference с area и одним маркером."""
        path = self._make_ref(
            "2026-07-09-002.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЛегкие без очаговых изменений.\n\nЗаключение:\nКТ-признаков патологии не выявлено.",
        )
        report = validate_reference(path)
        self.assertTrue(report.parseable)
        self.assertTrue(report.has_frontmatter)
        self.assertFalse(report.has_errors)
        self.assertEqual(report.task, "conclusion")
        self.assertEqual(report.areas, ["ОГК"])
        self.assertTrue(report.is_valid)
        self.assertEqual(report.recommended_action, "keep")

    def test_valid_description_reference(self):
        """Корректный description reference."""
        path = self._make_ref(
            "2026-07-10-001.md",
            {
                "область": ["ГМ"],
                "статус": True,
                "task": "description",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЧерепные структуры без изменений.",
        )
        report = validate_reference(path)
        self.assertFalse(report.has_errors)
        self.assertEqual(report.task, "description")
        self.assertTrue(report.is_valid)

    def test_valid_combined_reference(self):
        """Корректный description_and_conclusion reference."""
        path = self._make_ref(
            "2026-07-10-002.md",
            {
                "область": ["ОБП"],
                "статус": True,
                "task": "description_and_conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nПечень без очаговых образований.\n\nЗаключение:\nКТ-признаков патологии не выявлено.",
        )
        report = validate_reference(path)
        self.assertFalse(report.has_errors)
        self.assertEqual(report.task, "description_and_conclusion")

    # --- Вложённые дублирующие маркеры ---

    def test_nested_conclusion_marker_detected(self):
        """Два маркера 'Заключение' → ERROR."""
        path = self._make_ref(
            "2026-07-09-004.md",
            {
                "область": ["ГМ"],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nМозг без патологии.\n\nЗаключение:\nСтарое заключение.\n\nЗаключение:\nФинальное заключение.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("nested_conclusion_marker", codes)
        self.assertTrue(report.has_errors)

    def test_nested_description_marker_detected(self):
        """Два маркера 'Описание' → ERROR."""
        path = self._make_ref(
            "2026-07-13-001.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\n# Заголовок\n\nОписание:\nЛегкие без изменений.\n\nЗаключение:\nЗаключение текст.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("nested_description_marker", codes)
        self.assertTrue(report.has_errors)

    # --- Синтетические данные ---

    def test_synthetic_content_detected(self):
        """Синтетические формулировки → ERROR + recommended=rejected."""
        path = self._make_ref(
            "2026-07-12-001.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nСинтетическое описание: очаг S8 15x12 мм.\n\nЗаключение:\nСинтетическое заключение: динамика очага.",
        )
        report = validate_reference(path)
        self.assertTrue(report.is_synthetic)
        codes = [i.code for i in report.issues]
        self.assertIn("synthetic_content", codes)
        self.assertEqual(report.recommended_action, "rejected")

    # --- Отсутствие area для active reference ---

    def test_missing_area_for_active_reference(self):
        """Active reference без area → ERROR."""
        path = self._make_ref(
            "2026-07-13-003.md",
            {
                "область": [],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЛегкие чистые.\n\nЗаключение:\nБез патологии.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("missing_area", codes)
        self.assertTrue(report.has_errors)

    def test_missing_area_ok_for_needs_review(self):
        """needs_review reference без area → no error (не участвует в retrieval)."""
        path = self._make_ref(
            "2026-07-11-001.md",
            {
                "область": [],
                "статус": False,
                "task": "conclusion",
                "reference_status": "needs_review",
                "quality": "standard",
            },
            "Описание:\nЛегкие чистые.\n\nЗаключение:\nБез патологии.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertNotIn("missing_area", codes)

    # --- Отсутствие frontmatter ---

    def test_missing_frontmatter(self):
        """Файл без YAML frontmatter → ERROR."""
        path = self.tmp / "no_fm.md"
        path.write_text("Просто текст без frontmatter.", encoding="utf-8")
        report = validate_reference(path)
        self.assertFalse(report.has_frontmatter)
        codes = [i.code for i in report.issues]
        self.assertIn("missing_frontmatter", codes)

    # --- Запрещённые YAML-ключи ---

    def test_forbidden_keys_detected(self):
        """Запрещённые YAML-ключи (PHI) → ERROR."""
        path = self._make_ref(
            "2026-07-09-005.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
                "id": "123456",
                "врач": "Иванов И.И.",
            },
            "Описание:\nЛегкие без изменений.\n\nЗаключение:\nНорма.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("forbidden_keys", codes)

    # --- Task синонимы ---

    def test_task_alias_задача(self):
        """Task через русское поле 'задача'."""
        path = self._make_ref(
            "2026-07-09-010.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "задача": "заключение",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЛегкие без изменений.\n\nЗаключение:\nНорма.",
        )
        report = validate_reference(path)
        self.assertEqual(report.task, "conclusion")

    def test_unknown_task_warning(self):
        """Неизвестный task → WARNING."""
        path = self._make_ref(
            "2026-07-09-011.md",
            {
                "область": ["ОГК"],
                "статус": True,
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЛегкие без изменений.\n\nЗаключение:\nНорма.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("missing_task", codes)

    # --- Directory audit ---

    def test_validate_directory_counts(self):
        """Validate directory правильно считает valid/error/synthetic."""
        # Создаём 3 файла: один валидный, один с error, один синтетический
        self._make_ref(
            "good.md",
            {"область": ["ОГК"], "статус": True, "task": "conclusion",
             "reference_status": "active", "quality": "standard"},
            "Описание:\nТекст.\n\nЗаключение:\nЗаключение.",
        )
        self._make_ref(
            "synthetic.md",
            {"область": ["ОГК"], "статус": True, "task": "conclusion",
             "reference_status": "active", "quality": "standard"},
            "Описание:\nСинтетическое описание.\n\nЗаключение:\nСинтетическое заключение.",
        )
        self._make_ref(
            "bad.md",
            {"область": ["ГМ"], "статус": True, "task": "conclusion",
             "reference_status": "active", "quality": "standard"},
            "Описание:\nТекст.\n\nЗаключение:\nСтарое.\n\nЗаключение:\nФинальное.",
        )

        report = validate_directory(self.tmp)
        self.assertEqual(report.total, 3)
        self.assertEqual(report.valid_count, 1)
        self.assertEqual(report.synthetic_count, 1)
        self.assertEqual(report.error_count, 2)  # synthetic + nested marker

    def test_empty_directory(self):
        """Пустая директория → empty report."""
        report = validate_directory(self.tmp)
        self.assertEqual(report.total, 0)
        self.assertEqual(report.valid_count, 0)

    # --- JSON output ---

    def test_audit_report_to_dict(self):
        """AuditReport корректно сериализуется в dict."""
        self._make_ref(
            "good.md",
            {"область": ["ОГК"], "статус": True, "task": "conclusion",
             "reference_status": "active", "quality": "standard"},
            "Описание:\nТекст.\n\nЗаключение:\nЗаключение.",
        )
        report = validate_directory(self.tmp)
        d = report.to_dict()
        self.assertIn("total", d)
        self.assertIn("references", d)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["references"][0]["reference_id"], "good")
        self.assertTrue(d["references"][0]["is_valid"])

    # --- Status flag mismatch ---

    def test_status_flag_mismatch(self):
        """reference_status=active, но статус=false → ERROR."""
        path = self._make_ref(
            "2026-07-09-020.md",
            {
                "область": ["ОГК"],
                "статус": False,
                "task": "conclusion",
                "reference_status": "active",
                "quality": "standard",
            },
            "Описание:\nЛегкие.\n\nЗаключение:\nНорма.",
        )
        report = validate_reference(path)
        codes = [i.code for i in report.issues]
        self.assertIn("status_flag_mismatch", codes)


if __name__ == "__main__":
    unittest.main()