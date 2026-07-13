"""
Парсинг .md файлов базы референсов.
Формат: YAML frontmatter + тело с маркерами "Заключение:" и "Рекомендовано:"
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
import frontmatter

from .feedback_store import is_reference_active


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
        area = self.metadata.get("areas") or self.metadata.get("область", [])
        if isinstance(area, list):
            return area[0] if area else ""
        return str(area)

    @property
    def is_quality(self) -> bool:
        """Активный качественный reference идёт в few-shot."""
        return is_reference_active(self.metadata)

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


def _split_section(text: str, markers: list[str]) -> tuple[str, str]:
    """
    Разделяет текст по любому из маркеров.
    Маркеры могут иметь опциональный ## префикс и/или двоеточие.
    Возвращает (до, после).
    """
    for marker in markers:
        # Экранируем спецсимволы, разрешаем опциональный ## и :
        escaped = re.escape(marker)
        # Паттерн: ^##? marker :? \s* \n
        pattern = rf"^(?:##\s*)?{escaped}:?\s*\n"
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            before = text[:match.start()].strip()
            after = text[match.end():].strip()
            return before, after
    return text.strip(), ""


def _strip_code_blocks(text: str) -> str:
    """Удаляет markdown code-блоки, заголовки и дублирующие секционные маркеры."""
    # Удаляем заголовки ## Описание, ## Заключение и т.д.
    text = re.sub(r"^##\s+.*\s*$", "", text, flags=re.MULTILINE)
    # Удаляем code-блоки ```...```
    text = re.sub(r"^```[a-z]*\s*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    # Удаляем одиночные ``` в начале/конце
    lines = text.strip().split("\n")
    if lines and lines[0].strip() == "```":
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    cleaned = "\n".join(lines).strip()
    # Reference-файлы, созданные learning loop, хранят явные строки
    # "Описание:" / "Заключение:". prompt_builder сам добавляет эти подписи,
    # поэтому внутренний начальный маркер надо убрать, иначе получается
    # "Описание:\nОписание:" в few-shot prompt.
    cleaned = re.sub(
        r"^(?:Описание|Заключение|Рекомендовано|Рекомендации)\s*:\s*\n",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


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

        # V2 references используют task-aware секции вместо legacy-маркеров
        # «Описание/Заключение». Делегируем их единому schema parser, чтобы
        # найденный OHS reference не отбрасывался после retrieval.
        if int(metadata.get("schema_version", 1)) >= 2:
            from .reference_schema import parse_reference

            parsed = parse_reference(filepath)
            if not parsed or not parsed.source_input.strip():
                return None
            target = parsed.target_conclusion or parsed.target_description
            if not target.strip():
                return None
            return ReferenceEntry(
                filepath=str(filepath),
                metadata=metadata,
                description=parsed.source_input.strip(),
                conclusion=target.strip(),
                recommendation=parsed.target_recommendations.strip(),
            )

        # Разделяем: описание | заключение | рекомендовано
        # Маркеры в порядке приоритета (точные совпадения → вариации)
        description, rest = _split_section(body, ["Заключение", "## Заключение"])
        conclusion, recommendation = _split_section(rest, ["Рекомендовано", "## Рекомендации", "Рекомендации"])

        if not description or not conclusion:
            return None

        # Чистим markdown-обёртки (code-блоки, заголовки)
        description = _strip_code_blocks(description)
        conclusion = _strip_code_blocks(conclusion)
        recommendation = _strip_code_blocks(recommendation)

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