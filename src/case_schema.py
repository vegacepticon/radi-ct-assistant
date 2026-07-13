"""
Схемы данных для локального learning loop RadiCT Assistant.

Этот модуль не вызывает LLM и не работает с реальными внешними сервисами.
Он описывает только безопасный формат локальных case-файлов, feedback events
и reference-записей.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

TaskName = Literal[
    "conclusion",
    "description",
    "finding_description",
    "description_and_conclusion",
    "edit_description",
    "edit_conclusion",
]
InputType = Literal["text", "markdown", "voice_transcript"]
CaseStatus = Literal["draft", "accepted", "corrected"]


def is_clarification_response(text: str) -> bool:
    """True, если ответ — вопросы для диалога, а не финальная формулировка."""
    normalized = text.strip().lower().replace("ё", "е")
    return bool(re.match(r"^(?:#+\s*)?уточняющие\s+вопросы\s*:", normalized))

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# Назначение: вернуть текущее время в московском часовом поясе.
# Вход: ничего.
# Выход: ISO-строка вида "2026-07-06T12:00:00+03:00".
def now_moscow_iso() -> str:
    return datetime.now(MOSCOW_TZ).replace(microsecond=0).isoformat()


# Назначение: посчитать короткий стабильный SHA-256 хэш текста.
# Вход: произвольный текст, например черновик ассистента или финал Романа.
# Выход: первые 16 символов SHA-256; этого достаточно для локального журнала.
def short_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class CaseMetadata:
    """Метаданные рабочего случая без прямых идентификаторов пациента."""

    case_id: str
    created_at: str
    task: TaskName = "conclusion"
    input_type: InputType = "markdown"
    area: list[str] = field(default_factory=list)
    clinical_context: str = ""
    comparison: bool = False
    status: CaseStatus = "draft"
    references_used: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # Назначение: подготовить словарь для YAML frontmatter.
    # Вход: объект CaseMetadata.
    # Выход: dict с простыми типами, который можно безопасно отдать yaml.safe_dump().
    def to_frontmatter(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "case_id": self.case_id,
            "created_at": self.created_at,
            "task": self.task,
            "input_type": self.input_type,
            "область": self.area,
            "анамнез": self.clinical_context or None,
            "сравнение": self.comparison,
            "status": self.status,
        }
        if self.references_used:
            data["references_used"] = self.references_used
        data.update(self.extra)
        return data

    # Назначение: восстановить CaseMetadata из YAML frontmatter case-файла.
    # Вход: dict после yaml.safe_load().
    # Выход: объект CaseMetadata с нормализованными полями.
    @classmethod
    def from_frontmatter(cls, data: dict[str, Any]) -> "CaseMetadata":
        area = data.get("область", data.get("area", []))
        if isinstance(area, str):
            area = [area]
        clinical_context = data.get("анамнез", data.get("clinical_context", ""))
        known_keys = {
            "case_id",
            "created_at",
            "task",
            "input_type",
            "область",
            "area",
            "анамнез",
            "clinical_context",
            "сравнение",
            "comparison",
            "status",
            "references_used",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            case_id=str(data["case_id"]),
            created_at=str(data.get("created_at", now_moscow_iso())),
            task=data.get("task", "conclusion"),
            input_type=data.get("input_type", "markdown"),
            area=[str(item) for item in area],
            clinical_context="" if clinical_context is None else str(clinical_context),
            comparison=bool(data.get("сравнение", data.get("comparison", False))),
            status=data.get("status", "draft"),
            references_used=[str(item) for item in data.get("references_used", [])],
            extra=extra,
        )


@dataclass(slots=True)
class CaseRecord:
    """Полная рабочая запись: вход, черновик ассистента, финал Романа и feedback."""

    metadata: CaseMetadata
    input_text: str
    assistant_draft: str = ""
    roman_final: str = ""
    feedback: list[str] = field(default_factory=list)
    error_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeedbackEvent:
    """Одна JSONL-запись о принятии или исправлении case."""

    case_id: str
    created_at: str
    task: TaskName
    area: list[str]
    assistant_draft_hash: str
    roman_final_hash: str
    feedback: list[str] = field(default_factory=list)
    error_tags: list[str] = field(default_factory=list)
    promoted_to_reference: bool = False
    promoted_to_skill: bool = False

    # Назначение: подготовить event к записи в feedback_log.jsonl.
    # Вход: объект FeedbackEvent.
    # Выход: JSON-сериализуемый dict.
    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "created_at": self.created_at,
            "task": self.task,
            "area": self.area,
            "assistant_draft_hash": self.assistant_draft_hash,
            "roman_final_hash": self.roman_final_hash,
            "feedback": self.feedback,
            "error_tags": self.error_tags,
            "promoted_to_reference": self.promoted_to_reference,
            "promoted_to_skill": self.promoted_to_skill,
        }
