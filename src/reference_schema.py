"""
Task-aware reference schema v2 для RadiCT Assistant.

Определяет typed model для reference-файлов, где task определяет
структуру source/target блоков. Поддерживает backward compatibility
с legacy schema (v1: единая пара Описание → Заключение).

Контракты по задачам:
- conclusion:           source=готовое описание,  target=заключение
- description:          source=диктовка/находки,  target=описание
- description_and_conclusion: source=диктовка,     target=описание+заключение
- edit_description:     source=описание+указания,  target=описание
- edit_conclusion:      source=заключение+указания, target=заключение

V2 формат reference-файла:

```markdown
---
schema_version: 2
task: description
areas:
  - ОГК
comparison: true
reference_status: gold
quality: gold
style_version: 2026-07
input_kind: voice_transcript
output_mode: full_systematic
---

## Source input

[сырая диктовка / список находок / готовое описание]

## Target description

[финальное структурированное описание или пусто]

## Target conclusion

[финальное заключение или пусто]

## Target recommendations

[финальные рекомендации или пусто]
```

Legacy формат (v1) — без schema_version, с маркерами Описание:/Заключение: в теле.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import yaml


# --- Константы ---

SCHEMA_VERSION_V2 = 2

KNOWN_TASKS = {
    "conclusion",
    "description",
    "description_and_conclusion",
    "edit_description",
    "edit_conclusion",
}

# V2 section markers
SOURCE_INPUT_MARKER = "## Source input"
TARGET_DESCRIPTION_MARKER = "## Target description"
TARGET_CONCLUSION_MARKER = "## Target conclusion"
TARGET_RECOMMENDATIONS_MARKER = "## Target recommendations"

ALL_V2_MARKERS = [
    SOURCE_INPUT_MARKER,
    TARGET_DESCRIPTION_MARKER,
    TARGET_CONCLUSION_MARKER,
    TARGET_RECOMMENDATIONS_MARKER,
]

# Legacy v1 markers
LEGACY_DESCRIPTION_MARKERS = ["Описание", "## Описание"]
LEGACY_CONCLUSION_MARKERS = ["Заключение", "## Заключение"]
LEGACY_RECOMMENDATION_MARKERS = ["Рекомендовано", "## Рекомендации", "Рекомендации"]


# --- Dataclass ---

@dataclass
class TaskAwareReference:
    """
    Typed model reference-файла, поддерживающая v1 и v2 schema.

    Поля:
    - schema_version: 1 (legacy) или 2 (task-aware)
    - task: conclusion / description / description_and_conclusion / edit_*
    - areas: список областей (не single string)
    - source_input: входной текст (описание для conclusion, диктовка для description)
    - target_description: финальное описание (пусто для conclusion)
    - target_conclusion: финальное заключение (пусто для description)
    - target_recommendations: рекомендации (опционально)
    - metadata: полный YAML frontmatter dict
    - filepath: путь к файлу
    """
    schema_version: int = 1
    task: str = ""
    areas: list[str] = field(default_factory=list)
    source_input: str = ""
    target_description: str = ""
    target_conclusion: str = ""
    target_recommendations: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    filepath: str = ""

    @property
    def is_v2(self) -> bool:
        return self.schema_version >= SCHEMA_VERSION_V2

    @property
    def has_description_target(self) -> bool:
        """Task требует target description."""
        return self.task in ("description", "description_and_conclusion", "edit_description")

    @property
    def has_conclusion_target(self) -> bool:
        """Task требует target conclusion."""
        return self.task in ("conclusion", "description_and_conclusion", "edit_conclusion")

    def few_shot_block(self) -> str:
        """
        Сериализовать reference для few-shot prompt.

        Для v2: использует task-specific блоки.
        Для v1 (legacy): использует старый формат Описание → Заключение.
        """
        if self.is_v2:
            return self._few_shot_v2()
        return self._few_shot_v1()

    def _few_shot_v2(self) -> str:
        """V2 serialization: task-specific блоки."""
        parts = []
        parts.append("## Source input")
        parts.append(self.source_input.strip())

        if self.target_description.strip():
            parts.append("")
            parts.append("## Target description")
            parts.append(self.target_description.strip())

        if self.target_conclusion.strip():
            parts.append("")
            parts.append("## Target conclusion")
            parts.append(self.target_conclusion.strip())

        if self.target_recommendations.strip():
            parts.append("")
            parts.append("## Target recommendations")
            parts.append(self.target_recommendations.strip())

        return "\n".join(parts)

    def _few_shot_v1(self) -> str:
        """V1 (legacy) serialization: Описание → Заключение."""
        block = f"Описание:\n{self.source_input.strip()}\n\n"
        block += f"Заключение:\n{self.target_conclusion.strip()}"
        if self.target_recommendations.strip():
            block += f"\n\nРекомендовано:\n{self.target_recommendations.strip()}"
        return block


# --- Парсинг ---

def _split_section(text: str, markers: list[str]) -> tuple[str, str]:
    """
    Разделить текст по первому совпавшему маркеру.
    Возвращает (до, после).
    """
    for marker in markers:
        escaped = re.escape(marker)
        pattern = rf"^(?:##\s*)?{escaped}:?\s*\n"
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            before = text[:match.start()].strip()
            after = text[match.end():].strip()
            return before, after
    return text.strip(), ""


def _strip_code_blocks(text: str) -> str:
    """Удалить markdown code-блоки и заголовки."""
    text = re.sub(r"^##\s+.*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```[a-z]*\s*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    lines = text.strip().split("\n")
    if lines and lines[0].strip() == "```":
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    cleaned = "\n".join(lines).strip()
    # Убрать ведущий маркер секции, если он дублируется
    cleaned = re.sub(
        r"^(?:Описание|Заключение|Рекомендовано|Рекомендации)\s*:\s*\n",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def _split_v2_sections(body: str) -> dict[str, str]:
    """
    Разделить v2 body по ## markers.
    Возвращает dict: source_input / target_description / target_conclusion / target_recommendations.
    """
    marker_to_key = {
        SOURCE_INPUT_MARKER: "source_input",
        TARGET_DESCRIPTION_MARKER: "target_description",
        TARGET_CONCLUSION_MARKER: "target_conclusion",
        TARGET_RECOMMENDATIONS_MARKER: "target_recommendations",
    }
    # Паттерн для всех v2 маркеров
    pattern = re.compile(
        r"^## (Source input|Target description|Target conclusion|Target recommendations)[ \t]*$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        # match.group(0) — это "## Source input" без trailing newline
        marker_text = match.group(0).strip()
        key = marker_to_key.get(marker_text)
        if not key:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[key] = body[start:end].strip()
    return sections


def _resolve_task(metadata: dict[str, Any]) -> str:
    """Определить task из metadata (поля 'task' или 'задача')."""
    task = metadata.get("task") or metadata.get("задача") or ""
    task = str(task).strip().lower()
    if task in KNOWN_TASKS:
        return task
    # Синонимы
    aliases = {
        "заключение": "conclusion",
        "описание": "description",
        "описание_и_заключение": "description_and_conclusion",
    }
    return aliases.get(task, "")


def parse_reference(filepath: str | Path) -> TaskAwareReference | None:
    """
    Парсить reference-файл (v1 или v2).

    V2 определяется по наличию schema_version >= 2 в frontmatter.
    V1 — всё остальное (legacy Описание → Заключение формат).

    Возвращает TaskAwareReference или None, если файл не парсится.
    """
    filepath = Path(filepath)
    if not filepath.exists() or filepath.suffix != ".md":
        return None

    try:
        post = frontmatter.load(filepath)
        metadata = dict(post.metadata)
        body = post.content
    except Exception:
        return None

    schema_version = int(metadata.get("schema_version", 1))
    task = _resolve_task(metadata)

    # Areas: v2 uses 'areas', v1 uses 'область'
    areas = metadata.get("areas") or metadata.get("область") or []
    if isinstance(areas, str):
        areas = [areas]
    elif not isinstance(areas, list):
        areas = []

    ref = TaskAwareReference(
        schema_version=schema_version,
        task=task,
        areas=areas,
        metadata=metadata,
        filepath=str(filepath),
    )

    if schema_version >= SCHEMA_VERSION_V2:
        # V2: парсим по ## markers
        sections = _split_v2_sections(body)
        ref.source_input = sections.get("source_input", "")
        ref.target_description = sections.get("target_description", "")
        ref.target_conclusion = sections.get("target_conclusion", "")
        ref.target_recommendations = sections.get("target_recommendations", "")
    else:
        # V1 (legacy): парсим по Описание: / Заключение: маркерам
        description, rest = _split_section(body, LEGACY_CONCLUSION_MARKERS)
        conclusion, recommendation = _split_section(rest, LEGACY_RECOMMENDATION_MARKERS)

        description = _strip_code_blocks(description)
        conclusion = _strip_code_blocks(conclusion)
        recommendation = _strip_code_blocks(recommendation)

        # Для legacy: source_input = описание, target_conclusion = заключение
        ref.source_input = description
        ref.target_conclusion = conclusion
        ref.target_recommendations = recommendation
        # task по умолчанию для legacy — conclusion
        if not ref.task:
            ref.task = "conclusion"

    return ref


def render_v2_reference(
    task: str,
    areas: list[str],
    source_input: str,
    target_description: str = "",
    target_conclusion: str = "",
    target_recommendations: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Собрать v2 reference-файл из typed полей.

    Используется feedback_store._render_reference() для новых promotions.
    """
    now_prefix = metadata or {}
    fm = {
        "schema_version": SCHEMA_VERSION_V2,
        "task": task,
        "areas": areas,
        **now_prefix,
    }
    frontmatter_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()

    parts = [
        "---",
        frontmatter_text,
        "---",
        "",
        SOURCE_INPUT_MARKER,
        "",
        source_input.strip(),
    ]

    if target_description.strip():
        parts.extend(["", TARGET_DESCRIPTION_MARKER, "", target_description.strip()])

    if target_conclusion.strip():
        parts.extend(["", TARGET_CONCLUSION_MARKER, "", target_conclusion.strip()])

    if target_recommendations.strip():
        parts.extend(["", TARGET_RECOMMENDATIONS_MARKER, "", target_recommendations.strip()])

    parts.append("")
    return "\n".join(parts)