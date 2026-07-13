"""
Валидатор reference-файлов для RadiCT Assistant.

Модуль проверяет каждый .md файл в reference-vault на соответствие требованиям:
- наличие и корректность YAML frontmatter;
- известный task (conclusion / description / description_and_conclusion);
- непустая area для production (active/gold) reference;
- ровно один source/target contract (соответствие task → target блок);
- отсутствие вложенных дублирующих target-маркеров;
- active/gold reference парсится без неоднозначности;
- PHI guard (базовая проверка на прямые идентификаторы);
- синтетические тестовые данные не участвуют в production retrieval.

Валидатор НЕ мутирует файлы. Он только проверяет и возвращает структурированный
отчёт с списком найденных проблем и рекомендаций.

Использование:
    from src.reference_validator import validate_reference, validate_directory
    report = validate_directory("data/reference-vault/")
    print(report.to_json())
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


# --- Константы ---

KNOWN_TASKS = {
    "conclusion",
    "description",
    "description_and_conclusion",
    "edit_description",
    "edit_conclusion",
}

# Синонимы для task, которые могут встречаться в YAML как "задача"
TASK_ALIASES = {
    "заключение": "conclusion",
    "описание": "description",
    "описание_и_заключение": "description_and_conclusion",
    "description_and_conclusion": "description_and_conclusion",
}

# Допустимые lifecycle статусы
ACTIVE_STATUSES = {"active", "gold"}
# candidate — новый promotion, ещё не прошедший review
ALL_LIFECYCLE_STATUSES = {"candidate", "active", "gold", "deprecated", "needs_review", "rejected"}

# Допустимые quality значения
QUALITY_SCORES = {"gold", "high", "standard", "low"}

# Маркеры секций в теле reference
DESCRIPTION_MARKERS = ["Описание", "## Описание"]
CONCLUSION_MARKERS = ["Заключение", "## Заключение"]
RECOMMENDATION_MARKERS = ["Рекомендовано", "## Рекомендации", "Рекомендации"]

# Паттерны для определения синтетических данных
SYNTHETIC_MARKERS = [
    "Синтетическое описание",
    "Синтетическое заключение",
    "синтетический",
    "синтетическая",
    "синтетическое",
]

# Запрещенные YAML-ключи (PHI)
FORBIDDEN_KEYS = {
    "id",
    "patient_id",
    "пациент",
    "фио",
    "дата_рождения",
    "учреждение",
    "врач",
    "дата",
    "номер",
    "полис",
    "телефон",
}

# Паттерны прямых идентификаторов
DIRECT_ID_PATTERNS = [
    re.compile(r"\b\d{6,}\b"),  # длинные ID
    re.compile(r"\b\+?\d[\d\s()\-]{9,}\d\b"),  # телефоны
    re.compile(r"\b[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\b"),  # ФИО
]


# --- Типы результатов ---

class Severity(Enum):
    """Уровень серьёзности проблемы."""
    ERROR = "error"        # блокирует participation в retrieval
    WARNING = "warning"    # требует внимания, но не критично
    INFO = "info"          # информационное замечание


@dataclass
class ValidationIssue:
    """Одна найденная проблема в reference-файле."""
    severity: Severity
    code: str              # короткий код, например "nested_conclusion_marker"
    message: str            # человекочитаемое описание
    detail: str = ""        # дополнительный контекст (номер строки, фрагмент)


@dataclass
class ReferenceReport:
    """Отчёт по одному reference-файлу."""
    reference_id: str
    path: str
    issues: list[ValidationIssue] = field(default_factory=list)
    task: str = ""             # определённый task
    areas: list[str] = field(default_factory=list)
    reference_status: str = ""
    quality: str = ""
    body_length: int = 0
    is_synthetic: bool = False
    has_frontmatter: bool = False
    parseable: bool = True      # файл парсится без ошибок

    @property
    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARNING for i in self.issues)

    @property
    def is_valid(self) -> bool:
        """Reference может участвовать в production retrieval."""
        return self.parseable and not self.has_errors and not self.is_synthetic

    @property
    def recommended_action(self) -> str:
        """Рекомендуемое действие для reference."""
        if not self.parseable:
            return "needs_review"
        if self.is_synthetic:
            return "rejected"
        if self.has_errors:
            return "needs_review"
        if not self.areas and self.reference_status in ACTIVE_STATUSES:
            return "needs_review"
        return "keep"


@dataclass
class AuditReport:
    """Сводный отчёт по всем reference-файлам."""
    reports: list[ReferenceReport] = field(default_factory=list)
    total: int = 0
    valid_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    synthetic_count: int = 0
    needs_review_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в dict для JSON."""
        return {
            "total": self.total,
            "valid": self.valid_count,
            "errors": self.error_count,
            "warnings": self.warning_count,
            "synthetic": self.synthetic_count,
            "needs_review": self.needs_review_count,
            "references": [
                {
                    "reference_id": r.reference_id,
                    "path": r.path,
                    "task": r.task,
                    "areas": r.areas,
                    "reference_status": r.reference_status,
                    "quality": r.quality,
                    "body_length": r.body_length,
                    "is_synthetic": r.is_synthetic,
                    "parseable": r.parseable,
                    "has_frontmatter": r.has_frontmatter,
                    "is_valid": r.is_valid,
                    "recommended_action": r.recommended_action,
                    "issues": [
                        {
                            "severity": i.severity.value,
                            "code": i.code,
                            "message": i.message,
                            "detail": i.detail,
                        }
                        for i in r.issues
                    ],
                }
                for r in self.reports
            ],
        }


# --- Вспомогательные функции ---

def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """
    Разделить текст на YAML frontmatter и body.
    Возвращает (metadata, body, has_frontmatter).
    """
    if not text.startswith("---\n"):
        return {}, text, False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text, False
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text, False
    return metadata, parts[2].strip(), True


def _count_section_markers(body: str, markers: list[str]) -> int:
    """
    Посчитать количество секционных маркеров в теле.
    Маркеры могут быть с ## префиксом и/или двоеточием.
    """
    count = 0
    for marker in markers:
        escaped = re.escape(marker)
        pattern = rf"^(?:##\s*)?{escaped}:?\s*$"
        count += len(re.findall(pattern, body, re.MULTILINE))
    return count


def _resolve_task(metadata: dict[str, Any]) -> str:
    """
    Определить task из metadata.
    Проверяет поля 'task' и 'задача' (русский синоним).
    """
    task = metadata.get("task") or metadata.get("задача") or ""
    task = str(task).strip().lower()
    if task in KNOWN_TASKS:
        return task
    if task in TASK_ALIASES:
        return TASK_ALIASES[task]
    return ""


def _check_synthetic(body: str) -> bool:
    """Проверить, содержит ли тело синтетические/тестовые данные."""
    return any(marker.lower() in body.lower() for marker in SYNTHETIC_MARKERS)


def _check_forbidden_keys(metadata: dict[str, Any]) -> list[str]:
    """Найти запрещённые YAML-ключи в metadata."""
    return sorted(FORBIDDEN_KEYS.intersection(metadata.keys()))


def _check_direct_identifiers(text: str) -> str | None:
    """
    Проверить текст на прямые идентификаторы (PHI).
    Возвращает совпавший фрагмент или None.
    """
    for pattern in DIRECT_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


# --- Основная функция валидации ---

def validate_reference(filepath: str | Path) -> ReferenceReport:
    """
    Проверить один reference-файл.
    Возвращает ReferenceReport с найденными проблемами.
    """
    filepath = Path(filepath)
    report = ReferenceReport(
        reference_id=filepath.stem,
        path=str(filepath),
    )

    if not filepath.exists() or filepath.suffix != ".md":
        report.parseable = False
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "file_not_found",
            "Файл не существует или не является .md",
        ))
        return report

    try:
        text = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.parseable = False
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "encoding_error",
            "Файл не декодируется как UTF-8",
        ))
        return report

    # Разбор frontmatter
    metadata, body, has_fm = _split_frontmatter(text)
    report.has_frontmatter = has_fm
    report.body_length = len(body)

    if not has_fm:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "missing_frontmatter",
            "Отсутствует YAML frontmatter",
        ))
        report.parseable = False
        return report

    # Task
    task = _resolve_task(metadata)
    report.task = task
    if not task:
        report.issues.append(ValidationIssue(
            Severity.WARNING,
            "missing_task",
            "Task не указан (поля 'task' или 'задача' отсутствуют или неизвестны)",
        ))
    elif task not in KNOWN_TASKS:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "unknown_task",
            f"Неизвестный task: {task}",
        ))

    # Area
    area = metadata.get("область", [])
    if isinstance(area, str):
        area = [area]
    report.areas = area if isinstance(area, list) else []

    # Reference status
    ref_status = str(metadata.get("reference_status") or "active").lower()
    report.reference_status = ref_status
    if ref_status not in ALL_LIFECYCLE_STATUSES:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "invalid_reference_status",
            f"Недопустимый reference_status: {ref_status}",
        ))

    # Quality
    quality = str(metadata.get("quality") or "standard").lower()
    report.quality = quality
    if quality not in QUALITY_SCORES:
        report.issues.append(ValidationIssue(
            Severity.WARNING,
            "invalid_quality",
            f"Недопустимый quality: {quality}",
        ))

    # Проверка area для active/gold
    is_active = ref_status in ACTIVE_STATUSES
    if is_active and not report.areas:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "missing_area",
            "Active/gold reference должен иметь непустую область",
        ))

    # Проверка синтетических данных
    is_synthetic = _check_synthetic(body)
    report.is_synthetic = is_synthetic
    if is_synthetic:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "synthetic_content",
            "Файл содержит синтетические/тестовые данные",
        ))

    # Проверка дублирующих маркеров
    concl_count = _count_section_markers(body, CONCLUSION_MARKERS)
    desc_count = _count_section_markers(body, DESCRIPTION_MARKERS)

    if concl_count > 1:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "nested_conclusion_marker",
            f"Найдено {concl_count} маркеров 'Заключение' (ожидается 1)",
            f"Это приводит к вложённому дублированию в few-shot prompt",
        ))

    if desc_count > 1:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "nested_description_marker",
            f"Найдено {desc_count} маркеров 'Описание' (ожидается 1)",
            f"Это приводит к вложённому дублированию в few-shot prompt",
        ))

    # Проверка task/target contract
    if task == "conclusion":
        if concl_count == 0:
            report.issues.append(ValidationIssue(
                Severity.ERROR,
                "missing_conclusion_target",
                "Task=conclusion требует маркер 'Заключение' в теле",
            ))
    elif task == "description":
        if desc_count == 0:
            report.issues.append(ValidationIssue(
                Severity.ERROR,
                "missing_description_target",
                "Task=description требует маркер 'Описание' в теле",
            ))
    elif task == "description_and_conclusion":
        if desc_count == 0 or concl_count == 0:
            report.issues.append(ValidationIssue(
                Severity.ERROR,
                "missing_combined_targets",
                "Task=description_and_conclusion требует оба маркера: 'Описание' и 'Заключение'",
            ))

    # Проверка запрещённых YAML-ключей
    forbidden = _check_forbidden_keys(metadata)
    if forbidden:
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "forbidden_keys",
            f"Запрещённые YAML-ключи: {', '.join(forbidden)}",
            f"Эти ключи содержат PHI и не должны быть в reference",
        ))

    # Проверка прямых идентификаторов в теле
    phi_match = _check_direct_identifiers(body)
    if phi_match:
        report.issues.append(ValidationIssue(
            Severity.WARNING,
            "potential_phi",
            f"Потенциальный идентификатор в теле: {phi_match!r}",
        ))

    # Проверка статуса
    status_flag = metadata.get("статус")
    if status_flag is None:
        report.issues.append(ValidationIssue(
            Severity.WARNING,
            "missing_status_flag",
            "Отсутствует поле 'статус' (true/false) — нужно для is_reference_active()",
        ))
    elif is_active and not bool(status_flag):
        report.issues.append(ValidationIssue(
            Severity.ERROR,
            "status_flag_mismatch",
            "reference_status=active/gold, но статус=false — reference не попадёт в retrieval",
        ))

    return report


def validate_directory(directory: str | Path) -> AuditReport:
    """
    Проверить все .md файлы в директории.
    Возвращает сводный AuditReport.
    """
    directory = Path(directory)
    report = AuditReport()

    if not directory.exists():
        return report

    files = sorted(directory.glob("*.md"))
    for filepath in files:
        ref_report = validate_reference(filepath)
        report.reports.append(ref_report)
        report.total += 1
        if ref_report.is_valid:
            report.valid_count += 1
        if ref_report.has_errors:
            report.error_count += 1
        if ref_report.has_warnings:
            report.warning_count += 1
        if ref_report.is_synthetic:
            report.synthetic_count += 1
        if ref_report.recommended_action == "needs_review":
            report.needs_review_count += 1

    return report