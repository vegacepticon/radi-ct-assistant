import tempfile
import unittest
from pathlib import Path

from src.parser import parse_file


class ParserTest(unittest.TestCase):
    def test_learning_loop_reference_does_not_duplicate_section_labels_in_few_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference_path = Path(tmp) / "reference.md"
            reference_path.write_text(
                """---
статус: true
область:
- ОБП
---

Описание:
Синтетическое описание.

Заключение:
Синтетическое заключение.

Рекомендации:
Консультация специалиста.
""",
                encoding="utf-8",
            )

            entry = parse_file(reference_path)
            self.assertIsNotNone(entry)
            assert entry is not None
            few_shot = entry.few_shot_block()

            self.assertIn("Описание:\nСинтетическое описание.", few_shot)
            self.assertIn("Заключение:\nСинтетическое заключение.", few_shot)
            self.assertNotIn("Описание:\nОписание:", few_shot)
            self.assertNotIn("Заключение:\nЗаключение:", few_shot)


if __name__ == "__main__":
    unittest.main()
