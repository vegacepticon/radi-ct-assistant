"""
Парсинг .md файлов базы референсов.
Формат: YAML frontmatter + тело с маркерами "Заключение:" и "Рекомендовано:"
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
import frontmatter


@dataclass
class ReferenceEntry:
    """Распарсенный .md файл — одна пара описание → заключение."""
    filepath: str
    metadata: dict          # YAML frontmatter
    description: str        # тело до "Заключение:"
    conclusion: str         # после "Заключение:"
    recommendation: str = ""  # после "Рекомендовано:"

    @property
    def area(self) -> str:
        """Область исследования, e.g. 'КТА ГМ'."""
        area = self.metadata.get("область", [])
        if isinstance(area, list):
            return area[0] if area else ""
        return str(area)

    @property
    def is_quality(self) -> bool:
        """Статус: true → качественный, идёт в few-shot."""
        return bool(self.metadata.get("статус", False))

    @property
    def doctor(self) -> str:
        return str(self.metadata.get("врач", ""))

    def few_shot_block(self) -> str:
        """Готовый блок для few-shot промпта."""
        block = f"Описание:\n{self.description.strip()}\n\n"
        block += f"Заключение:\n{self.conclusion.strip()}"
        if self.recommendation:
            block += f"\n\nРекомендовано:\n{self.recommendation.strip()}"
        return block


def _split_section(text: str, marker: str) -> tuple[str, str]:
    """Разделяет текст по маркеру. Возвращает (до, после)."""
    pattern = rf"^{re.escape(marker)}\s*\n"
    match = re.search(pattern, text, re.MULTILINE)
    if match:
        before = text[:match.start()].strip()
        after = text[match.end():].strip()
        return before, after
    return text.strip(), ""


def parse_file(filepath: str | Path) -> ReferenceEntry | None:
    """
    Парсит один .md файл.
    Возвращает ReferenceEntry или None, если файл не валиден.
    """
    filepath = Path(filepath)
    if not filepath.exists() or filepath.suffix != ".md":
        return None

    try:
        post = frontmatter.load(filepath)
        metadata = dict(post.metadata)
        body = post.content

        # Разделяем: описание | заключение | рекомендовано
        description, rest = _split_section(body, "Заключение:")
        conclusion, recommendation = _split_section(rest, "Рекомендовано:")

        if not description or not conclusion:
            return None

        return ReferenceEntry(
            filepath=str(filepath),
            metadata=metadata,
            description=description,
            conclusion=conclusion,
            recommendation=recommendation,
        )
    except Exception as e:
        print(f"[parser] Error parsing {filepath}: {e}")
        return None


def parse_directory(directory: str | Path) -> list[ReferenceEntry]:
    """Парсит все .md файлы в директории (рекурсивно)."""
    directory = Path(directory)
    if not directory.exists():
        return []

    entries = []
    for filepath in sorted(directory.rglob("*.md")):
        entry = parse_file(filepath)
        if entry and entry.is_quality:
            entries.append(entry)
    return entries