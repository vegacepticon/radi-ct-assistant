"""
Тесты для task-aware reference schema v2.

Проверяют:
- парсинг v2 формата (## Source input / ## Target conclusion / etc)
- парсинг v1 legacy формата (Описание: / Заключение:)
- рендеринг v2 через render_v2_reference()
- few_shot_block() для v1 и v2
- task contracts (conclusion, description, description_and_conclusion)
"""
import tempfile
import unittest
from pathlib import Path

from src.reference_schema import (
    TaskAwareReference,
    parse_reference,
    render_v2_reference,
    SCHEMA_VERSION_V2,
)


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestReferenceSchemaV2(unittest.TestCase):
    """Тесты v2 schema — task-aware format."""

    def test_parse_v2_conclusion(self):
        """V2 reference с task=conclusion парсится правильно."""
        content = """---
schema_version: 2
task: conclusion
areas:
  - ОГК
reference_status: active
quality: standard
---

## Source input

Описание: легкие без очаговых изменений.

## Target conclusion

КТ-признаков патологии не выявлено.
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertEqual(ref.schema_version, 2)
        self.assertTrue(ref.is_v2)
        self.assertEqual(ref.task, "conclusion")
        self.assertEqual(ref.areas, ["ОГК"])
        self.assertIn("легкие", ref.source_input)
        self.assertEqual(ref.target_description, "")
        self.assertIn("КТ-признаков", ref.target_conclusion)

    def test_parse_v2_description(self):
        """V2 reference с task=description: target_description, пустой conclusion."""
        content = """---
schema_version: 2
task: description
areas:
  - ГМ
reference_status: active
quality: standard
---

## Source input

Диктовка: черепные структуры без изменений.

## Target description

МСКТ-исследование головного мозга. Черепные структуры без изменений.
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertEqual(ref.task, "description")
        self.assertTrue(ref.has_description_target)
        self.assertFalse(ref.has_conclusion_target)
        self.assertIn("Диктовка", ref.source_input)
        self.assertIn("МСКТ-исследование", ref.target_description)
        self.assertEqual(ref.target_conclusion, "")

    def test_parse_v2_combined(self):
        """V2 reference с task=description_and_conclusion: оба target блока."""
        content = """---
schema_version: 2
task: description_and_conclusion
areas:
  - ОГК
reference_status: active
quality: standard
---

## Source input

Диктовка: очаг S8 правого легкого 15 мм.

## Target description

В S8 правого легкого очаг 15 мм.

## Target conclusion

Очаг S8 правого легкого.
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertEqual(ref.task, "description_and_conclusion")
        self.assertTrue(ref.has_description_target)
        self.assertTrue(ref.has_conclusion_target)
        self.assertIn("Диктовка", ref.source_input)
        self.assertIn("S8", ref.target_description)
        self.assertIn("Очаг S8", ref.target_conclusion)

    def test_parse_v2_with_recommendations(self):
        """V2 reference с recommendations."""
        content = """---
schema_version: 2
task: conclusion
areas:
  - ОГК
---

## Source input

Описание текст.

## Target conclusion

Заключение текст.

## Target recommendations

Консультация специалиста.
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertIn("Консультация", ref.target_recommendations)

    def test_few_shot_v2_conclusion(self):
        """V2 few_shot_block для conclusion: Source + Target conclusion."""
        ref = TaskAwareReference(
            schema_version=2,
            task="conclusion",
            areas=["ОГК"],
            source_input="Описание текст.",
            target_conclusion="Заключение текст.",
        )
        block = ref.few_shot_block()
        self.assertIn("## Source input", block)
        self.assertIn("Описание текст.", block)
        self.assertIn("## Target conclusion", block)
        self.assertIn("Заключение текст.", block)
        # Нет target description для conclusion
        self.assertNotIn("## Target description", block)

    def test_few_shot_v2_description(self):
        """V2 few_shot_block для description: Source + Target description, без conclusion."""
        ref = TaskAwareReference(
            schema_version=2,
            task="description",
            areas=["ГМ"],
            source_input="Диктовка текст.",
            target_description="Структурированное описание.",
        )
        block = ref.few_shot_block()
        self.assertIn("## Source input", block)
        self.assertIn("Диктовка текст.", block)
        self.assertIn("## Target description", block)
        self.assertIn("Структурированное описание.", block)
        self.assertNotIn("## Target conclusion", block)

    def test_few_shot_v2_combined(self):
        """V2 few_shot_block для combined: Source + Target description + Target conclusion."""
        ref = TaskAwareReference(
            schema_version=2,
            task="description_and_conclusion",
            areas=["ОГК"],
            source_input="Диктовка.",
            target_description="Описание.",
            target_conclusion="Заключение.",
        )
        block = ref.few_shot_block()
        self.assertIn("## Source input", block)
        self.assertIn("## Target description", block)
        self.assertIn("## Target conclusion", block)


class TestReferenceSchemaV1Legacy(unittest.TestCase):
    """Тесты v1 legacy schema — backward compatibility."""

    def test_parse_v1_legacy(self):
        """V1 legacy reference парсится с task=conclusion по умолчанию."""
        content = """---
область:
  - ОГК
статус: true
задача: conclusion
reference_status: active
quality: standard
---

Описание:
Легкие без изменений.

Заключение:
Норма.
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertEqual(ref.schema_version, 1)
        self.assertFalse(ref.is_v2)
        self.assertEqual(ref.task, "conclusion")
        self.assertIn("Легкие", ref.source_input)
        self.assertIn("Норма", ref.target_conclusion)
        self.assertEqual(ref.target_description, "")

    def test_few_shot_v1_legacy(self):
        """V1 few_shot_block: Описание: → Заключение: формат."""
        ref = TaskAwareReference(
            schema_version=1,
            task="conclusion",
            source_input="Описание текст.",
            target_conclusion="Заключение текст.",
        )
        block = ref.few_shot_block()
        self.assertIn("Описание:", block)
        self.assertIn("Описание текст.", block)
        self.assertIn("Заключение:", block)
        self.assertIn("Заключение текст.", block)
        # V2 markers не должны быть в v1 output
        self.assertNotIn("## Source input", block)
        self.assertNotIn("## Target conclusion", block)


class TestRenderV2(unittest.TestCase):
    """Тесты render_v2_reference()."""

    def test_render_conclusion(self):
        """Рендеринг v2 conclusion reference."""
        text = render_v2_reference(
            task="conclusion",
            areas=["ОГК"],
            source_input="Описание: легкие чистые.",
            target_conclusion="Норма.",
        )
        self.assertIn("schema_version: 2", text)
        self.assertIn("task: conclusion", text)
        self.assertIn("## Source input", text)
        self.assertIn("## Target conclusion", text)
        self.assertNotIn("## Target description", text)

    def test_render_description(self):
        """Рендеринг v2 description reference."""
        text = render_v2_reference(
            task="description",
            areas=["ГМ"],
            source_input="Диктовка: мозг без патологии.",
            target_description="МСКТ головного мозга. Без патологии.",
        )
        self.assertIn("## Source input", text)
        self.assertIn("## Target description", text)
        self.assertNotIn("## Target conclusion", text)

    def test_render_combined(self):
        """Рендеринг v2 combined reference."""
        text = render_v2_reference(
            task="description_and_conclusion",
            areas=["ОГК"],
            source_input="Диктовка: очаг S8.",
            target_description="В S8 правого легкого очаг.",
            target_conclusion="Очаг S8 правого легкого.",
            target_recommendations="Контроль через 6 месяцев.",
        )
        self.assertIn("## Source input", text)
        self.assertIn("## Target description", text)
        self.assertIn("## Target conclusion", text)
        self.assertIn("## Target recommendations", text)

    def test_render_with_metadata(self):
        """Рендеринг с дополнительной metadata."""
        text = render_v2_reference(
            task="conclusion",
            areas=["ОГК"],
            source_input="Текст.",
            target_conclusion="Заключение.",
            metadata={
                "reference_status": "gold",
                "quality": "gold",
                "style_version": "2026-07",
            },
        )
        self.assertIn("reference_status: gold", text)
        self.assertIn("quality: gold", text)

    def test_round_trip(self):
        """Round-trip: render → parse → проверить поля."""
        original = render_v2_reference(
            task="description_and_conclusion",
            areas=["ОГК", "ОБП"],
            source_input="Диктовка.",
            target_description="Описание.",
            target_conclusion="Заключение.",
        )
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(original)
            f.flush()
            ref = parse_reference(f.name)

        self.assertIsNotNone(ref)
        self.assertTrue(ref.is_v2)
        self.assertEqual(ref.task, "description_and_conclusion")
        self.assertEqual(ref.areas, ["ОГК", "ОБП"])
        self.assertIn("Диктовка", ref.source_input)
        self.assertIn("Описание", ref.target_description)
        self.assertIn("Заключение", ref.target_conclusion)


class TestTaskContracts(unittest.TestCase):
    """Тесты task contracts — has_description_target / has_conclusion_target."""

    def test_conclusion_contract(self):
        ref = TaskAwareReference(task="conclusion")
        self.assertFalse(ref.has_description_target)
        self.assertTrue(ref.has_conclusion_target)

    def test_description_contract(self):
        ref = TaskAwareReference(task="description")
        self.assertTrue(ref.has_description_target)
        self.assertFalse(ref.has_conclusion_target)

    def test_combined_contract(self):
        ref = TaskAwareReference(task="description_and_conclusion")
        self.assertTrue(ref.has_description_target)
        self.assertTrue(ref.has_conclusion_target)

    def test_edit_description_contract(self):
        ref = TaskAwareReference(task="edit_description")
        self.assertTrue(ref.has_description_target)
        self.assertFalse(ref.has_conclusion_target)

    def test_edit_conclusion_contract(self):
        ref = TaskAwareReference(task="edit_conclusion")
        self.assertFalse(ref.has_description_target)
        self.assertTrue(ref.has_conclusion_target)


if __name__ == "__main__":
    unittest.main()