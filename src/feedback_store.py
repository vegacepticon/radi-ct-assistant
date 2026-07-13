"""
Локальное хранилище learning loop для RadiCT Assistant.

Что делает модуль:
- сохраняет рабочие case-файлы в data/cases;
- пишет журнал feedback events в data/feedback/feedback_log.jsonl;
- создает lesson candidates;
- по явной команде переносит проверенный case в reference base.

Важно: модуль не вызывает внешние LLM/API и не должен использоваться для
реальных неанонимизированных данных. Promotion в reference base выполняется
только после базовой PHI-проверки и явной команды CLI/API.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .case_schema import (
    CaseMetadata,
    CaseRecord,
    FeedbackEvent,
    InputType,
    TaskName,
    now_moscow_iso,
    short_text_hash,
)
from .audit_log import AuditLog
from .config import AUTO_REINDEX_REFERENCES, BASE_DIR, REFERENCE_VAULT_DIR, REFERENCES_DIR

CASE_SECTIONS = {
    "input": "## Input",
    "assistant_draft": "## Assistant draft",
    "roman_final": "## Roman final",
    "feedback": "## Feedback",
    "error_tags": "## Error tags",
}

DIRECT_IDENTIFIER_PATTERNS = [
    re.compile(r"\b\d{6,}\b"),  # длинные ID/номера исследований
    re.compile(r"\b\+?\d[\d\s()\-]{9,}\d\b"),  # телефоны
    # Клинические даты операций/исследований в теле протокола не считаем
    # прямым идентификатором сами по себе: они нужны для динамики и анамнеза.
    # Запрещенные YAML-ключи с административными датами по-прежнему блокируются
    # через FORBIDDEN_REFERENCE_KEYS.
    re.compile(r"\b[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\b"),  # ФИО целиком
]

FORBIDDEN_REFERENCE_KEYS = {
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

REFERENCE_ACTIVE_STATUSES = {"active", "gold"}
# Phase 7: candidate — новый promotion получает этот статус, не active.
# Только после review можно повысить до active/gold.
REFERENCE_ALL_STATUSES = {"candidate", "active", "gold", "deprecated", "needs_review", "rejected"}
REFERENCE_QUALITY_SCORES = {"gold": 1.0, "high": 0.85, "standard": 0.65, "low": 0.35}


# --- Structured outcome types for promotion/reindex ---

@dataclass(slots=True)
class ReindexResult:
    """Результат best-effort переиндексации OHS.

    Помогает отличить полный success от partial failure (reference сохранён,
    но индекс не обновлён) и не маскировать ошибки.

    Поля:
    - success: True, если reindex прошёл без ошибок.
    - error: пустая строка при success, сообщение об ошибке при failure.
    - indexed: количество проиндексированных notes после reindex (best-effort).
    """

    success: bool
    error: str = ""
    indexed: int = 0


@dataclass(slots=True)
class PromotionResult:
    """Структурированный результат promotion case → reference.

    Позволяет API и CLI сообщать фактический outcome, а не только намерение.

    Поля:
    - saved: True, если reference-файл записан на диск и проверен.
    - reference_id: case_id, использованный как имя reference-файла.
    - path: путь к reference-файлу в reference-vault (пустой, если не saved).
    - legacy_path: путь к совместимому mirror в data/references.
    - index_updated: True, если OHS reindex прошёл успешно.
    - index_error: сообщение об ошибке reindex (пустой, если успешно).
    - skip_reason: причина, если promotion не выполнялся (например, save_as_reference=False).
    """

    saved: bool
    reference_id: str = ""
    path: str = ""
    legacy_path: str = ""
    index_updated: bool = False
    index_error: str = ""
    skip_reason: str = ""


# Назначение: безопасно разобрать ISO datetime из YAML metadata.
# Вход: строка created_at/updated_at или пустое значение.
# Выход: datetime или None, если формат неизвестен.
def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return datetime.fromisoformat(f"{text}-01T00:00:00+03:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# Назначение: вычислить числовой приоритет reference для сортировки.
# Вход: YAML metadata reference-файла.
# Выход: score, где выше = предпочтительнее. Semantic score добавляется отдельно в retriever.
def reference_lifecycle_score(metadata: dict[str, Any]) -> float:
    status = str(metadata.get("reference_status") or "active").lower()
    quality = str(metadata.get("quality") or ("gold" if status == "gold" else "standard")).lower()
    score = REFERENCE_QUALITY_SCORES.get(quality, REFERENCE_QUALITY_SCORES["standard"])
    if status == "gold":
        score += 0.25
    elif status == "needs_review":
        score -= 0.25

    dt = _parse_iso_datetime(metadata.get("updated_at") or metadata.get("created_at"))
    if dt:
        # Мягкий recency bonus: новые примеры получают преимущество, но хороший
        # старый gold-reference не уничтожается только из-за возраста.
        age_days = max((datetime.now(dt.tzinfo) - dt).days, 0)
        score += max(0.0, 0.25 - min(age_days, 365) / 365 * 0.25)
    return score


# Назначение: проверить, может ли reference участвовать в few-shot retrieval.
# Вход: YAML metadata.
# Выход: True только для статус:true и active/gold lifecycle.
def is_reference_active(metadata: dict[str, Any]) -> bool:
    if not bool(metadata.get("статус", False)):
        return False
    status = str(metadata.get("reference_status") or "active").lower()
    return status in REFERENCE_ACTIVE_STATUSES


@dataclass(slots=True)
class FeedbackStore:
    """Файловое хранилище case/feedback/reference данных."""

    base_dir: Path = BASE_DIR

    # Назначение: вычислить основные директории и убедиться, что они существуют.
    # Вход: объект FeedbackStore с base_dir.
    # Выход: созданы data/cases/* и data/feedback/*, если их еще не было.
    def ensure_dirs(self) -> None:
        for path in [
            self.cases_dir,
            self.drafts_dir,
            self.accepted_dir,
            self.corrected_dir,
            self.feedback_dir,
            self.lesson_candidates_dir,
            self.references_dir,
            self.reference_vault_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    @property
    def drafts_dir(self) -> Path:
        return self.cases_dir / "drafts"

    @property
    def accepted_dir(self) -> Path:
        return self.cases_dir / "accepted"

    @property
    def corrected_dir(self) -> Path:
        return self.cases_dir / "corrected"

    @property
    def feedback_dir(self) -> Path:
        return self.data_dir / "feedback"

    @property
    def feedback_log_path(self) -> Path:
        return self.feedback_dir / "feedback_log.jsonl"

    @property
    def lesson_candidates_dir(self) -> Path:
        return self.feedback_dir / "lesson_candidates"

    @property
    def references_dir(self) -> Path:
        """Legacy reference directory kept for compatibility with older tooling."""
        if self.base_dir == BASE_DIR:
            return REFERENCES_DIR
        return self.base_dir / "data" / "references"

    @property
    def reference_vault_dir(self) -> Path:
        """Obsidian-like vault used by the OHS RAG backend."""
        if self.base_dir == BASE_DIR:
            return REFERENCE_VAULT_DIR
        return self.base_dir / "data" / "reference-vault"

    @property
    def audit_log(self) -> AuditLog:
        """Локальный аудит-журнал RadiCT событий без медицинского текста."""
        return AuditLog(base_dir=self.base_dir)

    # Назначение: создать новый case_id с датой и порядковым номером за день.
    # Вход: текущее содержимое data/cases.
    # Выход: строка вида "2026-07-06-001".
    def next_case_id(self) -> str:
        self.ensure_dirs()
        today = now_moscow_iso()[:10]
        existing = list(self.cases_dir.glob(f"**/{today}-*.md"))
        return f"{today}-{len(existing) + 1:03d}"

    # Назначение: создать рабочий case с входным текстом и опциональным черновиком.
    # Вход: input_text, assistant_draft и безопасные метаданные.
    # Выход: CaseRecord; файл появляется в data/cases/drafts/<case_id>.md.
    def create_case(
        self,
        input_text: str,
        assistant_draft: str = "",
        task: TaskName = "conclusion",
        input_type: InputType = "markdown",
        area: list[str] | None = None,
        clinical_context: str = "",
        comparison: bool = False,
        references_used: list[str] | None = None,
    ) -> CaseRecord:
        case_id = self.next_case_id()
        metadata = CaseMetadata(
            case_id=case_id,
            created_at=now_moscow_iso(),
            task=task,
            input_type=input_type,
            area=area or [],
            clinical_context=clinical_context,
            comparison=comparison,
            status="draft",
            references_used=references_used or [],
        )
        record = CaseRecord(
            metadata=metadata,
            input_text=input_text.strip(),
            assistant_draft=assistant_draft.strip(),
        )
        self.save_draft(record)
        self.audit_log.log_event(
            "draft_created",
            case_id=case_id,
            task=task,
        )
        return record

    # Назначение: сохранить case в папку drafts.
    # Вход: CaseRecord со status=draft.
    # Выход: путь к markdown-файлу.
    def save_draft(self, record: CaseRecord) -> Path:
        record.metadata.status = "draft"
        return self._write_case(record, self.drafts_dir / f"{record.metadata.case_id}.md")

    # Назначение: принять черновик без правок и перенести case в accepted.
    # Вход: case_id; опционально save_as_reference=True для promotion.
    # Выход: кортеж (обновленный CaseRecord, PromotionResult | None).
    # PromotionResult равен None, если save_as_reference=False.
    def accept_case(
        self, case_id: str, save_as_reference: bool = False
    ) -> tuple[CaseRecord, PromotionResult | None]:
        record = self.load_case(case_id)
        final_text = record.roman_final or record.assistant_draft
        record.roman_final = final_text.strip()
        record.metadata.status = "accepted"
        self._write_case(record, self.accepted_dir / f"{case_id}.md")
        self._remove_other_case_copies(case_id, keep_dir=self.accepted_dir)
        self.append_feedback_event(
            record,
            promoted_to_reference=False,
            promoted_to_skill=False,
        )
        promotion_result = None
        if save_as_reference:
            promotion_result = self.promote_to_reference(case_id)
        self.audit_log.log_event("accepted", case_id=case_id)
        return record, promotion_result

    # Назначение: сохранить исправленный Романом финал и feedback.
    # Вход: case_id, final text, список feedback-пунктов и error tags.
    # Выход: обновленный CaseRecord в data/cases/corrected.
    def correct_case(
        self,
        case_id: str,
        roman_final: str,
        feedback: list[str] | None = None,
        error_tags: list[str] | None = None,
        save_as_reference: bool = False,
        create_lesson_candidate: bool = False,
    ) -> tuple[CaseRecord, PromotionResult | None]:
        record = self.load_case(case_id)
        record.roman_final = roman_final.strip()
        record.feedback = feedback or []
        record.error_tags = error_tags or []
        record.metadata.status = "corrected"
        self._write_case(record, self.corrected_dir / f"{case_id}.md")
        self._remove_other_case_copies(case_id, keep_dir=self.corrected_dir)
        self.append_feedback_event(
            record,
            promoted_to_reference=False,
            promoted_to_skill=create_lesson_candidate,
        )
        if create_lesson_candidate:
            self.create_lesson_candidate(record)
        promotion_result = None
        if save_as_reference:
            promotion_result = self.promote_to_reference(case_id)
        self.audit_log.log_event("corrected", case_id=case_id)
        return record, promotion_result

    # Назначение: добавить запись в data/feedback/feedback_log.jsonl.
    # Вход: CaseRecord и флаги promotion.
    # Выход: FeedbackEvent; строка JSON добавлена в лог.
    def append_feedback_event(
        self,
        record: CaseRecord,
        promoted_to_reference: bool = False,
        promoted_to_skill: bool = False,
    ) -> FeedbackEvent:
        self.ensure_dirs()
        event = FeedbackEvent(
            case_id=record.metadata.case_id,
            created_at=now_moscow_iso(),
            task=record.metadata.task,
            area=record.metadata.area,
            assistant_draft_hash=short_text_hash(record.assistant_draft),
            roman_final_hash=short_text_hash(record.roman_final),
            feedback=record.feedback,
            error_tags=record.error_tags,
            promoted_to_reference=promoted_to_reference,
            promoted_to_skill=promoted_to_skill,
        )
        with self.feedback_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event

    # Назначение: создать markdown-файл candidate rule для ручного переноса в skill.
    # Вход: исправленный CaseRecord с feedback/error_tags.
    # Выход: путь к файлу в data/feedback/lesson_candidates.
    #
    # Phase 7: provenance — сохраняем source case ID, дату, task, area
    # для audit trail при переносе правила в skill.
    def create_lesson_candidate(self, record: CaseRecord) -> Path:
        self.ensure_dirs()
        path = self.lesson_candidates_dir / f"{record.metadata.case_id}.md"
        lines = [
            f"# Lesson candidate {record.metadata.case_id}",
            "",
            f"**Source case:** {record.metadata.case_id}",
            f"**Date:** {now_moscow_iso()[:10]}",
            f"**Task:** {record.metadata.task}",
            f"**Area:** {', '.join(record.metadata.area) if record.metadata.area else '-'}",
            f"**Status:** unconfirmed",
            "",
            "## Feedback",
            *[f"- {item}" for item in record.feedback],
            "",
            "## Error tags",
            *[f"- {tag}" for tag in record.error_tags],
            "",
            "## Skill transfer criteria",
            "- [ ] Rule is generalizable (not case-specific)",
            "- [ ] Repeated or explicitly confirmed by Roman",
            "- [ ] Does not depend on one specific image",
            "- [ ] Does not contradict existing rules",
            "",
            "## Provenance",
            f"- source_case: {record.metadata.case_id}",
            f"- created_at: {now_moscow_iso()[:7]}",
            f"- feedback_hash: {short_text_hash(chr(10).join(record.feedback))}",
        ]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    # Назначение: перенести accepted/corrected case в reference base для few-shot.
    # Вход: case_id уже принятого или исправленного case.
    # Выход: PromotionResult с проверенным saved/index_updated и путями.
    # Ошибка reindex не маскируется: возвращается в index_error как partial failure.
    def promote_to_reference(
        self,
        case_id: str,
        reference_status: str = "candidate",
        quality: str = "standard",
        style_version: str | None = None,
    ) -> PromotionResult:
        record = self.load_case(case_id)
        if record.metadata.status not in ("accepted", "corrected"):
            raise ValueError("Only accepted/corrected cases can be promoted to references")
        if not record.roman_final.strip():
            raise ValueError("Cannot promote case without Roman final text")

        reference_text = self._render_reference(
            record,
            reference_status=reference_status,
            quality=quality,
            style_version=style_version,
        )
        self.assert_no_direct_identifiers(reference_text)
        self.ensure_reference_frontmatter_safe(record.metadata)

        path = self.reference_vault_dir / f"{case_id}.md"
        path.write_text(reference_text, encoding="utf-8")

        # Legacy mirror: старый Chroma/parse_directory path оставляем рабочим,
        # но основным источником RAG становится Obsidian-like reference vault.
        legacy_path = self.references_dir / f"{case_id}.md"
        legacy_path.write_text(reference_text, encoding="utf-8")

        # Verify: reference-файл действительно записан на диск.
        if not path.exists():
            raise RuntimeError(f"Reference file was not written: {path}")
        if not legacy_path.exists():
            raise RuntimeError(f"Legacy mirror file was not written: {legacy_path}")

        index_updated = False
        index_error = ""
        if AUTO_REINDEX_REFERENCES:
            reindex_result = self.reindex_reference_vault_best_effort()
            index_updated = reindex_result.success
            index_error = reindex_result.error

        self.append_feedback_event(record, promoted_to_reference=True, promoted_to_skill=False)
        self.audit_log.log_event(
            "reference_saved",
            case_id=case_id,
            reference_id=case_id,
        )
        if index_updated:
            self.audit_log.log_event("index_updated", reference_id=case_id)
        else:
            self.audit_log.log_event(
                "capture_failed",
                case_id=case_id,
                reason_code="reindex_failed",
            )
        return PromotionResult(
            saved=True,
            reference_id=case_id,
            path=str(path),
            legacy_path=str(legacy_path),
            index_updated=index_updated,
            index_error=index_error,
        )

    # Назначение: переиндексировать OHS index и вернуть структурированный результат.
    # Вход: текущий reference_vault_dir.
    # Выход: ReindexResult с success=True/False и сообщением об ошибке.
    # Ошибка reindex больше не маскируется: вызывающий код получает результат
    # и может сообщить partial failure.
    def reindex_reference_vault_best_effort(self) -> ReindexResult:
        try:
            from .ohs import ohs_reindex

            status = ohs_reindex(vault_dir=self.reference_vault_dir, force=False)
            return ReindexResult(success=True, error="", indexed=status.indexed)
        except Exception as e:
            return ReindexResult(success=False, error=str(e), indexed=0)

    # Назначение: прочитать case из любой статусной папки.
    # Вход: case_id.
    # Выход: CaseRecord.
    def load_case(self, case_id: str) -> CaseRecord:
        self.ensure_dirs()
        for directory in [self.corrected_dir, self.accepted_dir, self.drafts_dir]:
            path = directory / f"{case_id}.md"
            if path.exists():
                return self._read_case(path)
        raise FileNotFoundError(f"Case not found: {case_id}")

    # Назначение: вывести список существующих cases.
    # Вход: опциональный status-фильтр.
    # Выход: список словарей для CLI/API.
    def list_cases(self, status: str | None = None) -> list[dict[str, Any]]:
        self.ensure_dirs()
        dirs = {
            "draft": self.drafts_dir,
            "accepted": self.accepted_dir,
            "corrected": self.corrected_dir,
        }
        selected = {status: dirs[status]} if status in dirs else dirs
        items: list[dict[str, Any]] = []
        for status_name, directory in selected.items():
            for path in sorted(directory.glob("*.md")):
                record = self._read_case(path)
                items.append(
                    {
                        "case_id": record.metadata.case_id,
                        "status": status_name,
                        "task": record.metadata.task,
                        "area": record.metadata.area,
                        "created_at": record.metadata.created_at,
                        "path": str(path),
                    }
                )
        return sorted(items, key=lambda item: item["case_id"])

    # Назначение: вывести references с lifecycle metadata для ревизии базы.
    # Вход: опциональный status-фильтр; include_inactive=True показывает все.
    # Выход: список словарей с path/status/quality/style_version/created_at.
    def list_references(
        self,
        status: str | None = None,
        include_inactive: bool = True,
    ) -> list[dict[str, Any]]:
        self.ensure_dirs()
        items: list[dict[str, Any]] = []
        for path in sorted(self.reference_vault_dir.glob("*.md")):
            metadata, _body = self._split_frontmatter(path.read_text(encoding="utf-8"))
            reference_status = str(metadata.get("reference_status") or "active")
            if status and reference_status != status:
                continue
            if not include_inactive and reference_status not in REFERENCE_ACTIVE_STATUSES:
                continue
            items.append(
                {
                    "reference_id": path.stem,
                    "path": str(path),
                    "reference_status": reference_status,
                    "quality": str(metadata.get("quality") or "standard"),
                    "style_version": str(metadata.get("style_version") or ""),
                    "task": str(metadata.get("задача") or ""),
                    "area": metadata.get("область") or [],
                    "created_at": str(metadata.get("created_at") or ""),
                    "updated_at": str(metadata.get("updated_at") or ""),
                    "lifecycle_score": round(reference_lifecycle_score(metadata), 4),
                }
            )
        return sorted(items, key=lambda item: (item["reference_status"], item["reference_id"]))

    # Назначение: обновить lifecycle metadata существующего reference.
    # Вход: reference_id/case_id и новые status/quality/style_version.
    # Выход: путь к обновленному reference; legacy mirror обновляется тоже.
    def update_reference_lifecycle(
        self,
        reference_id: str,
        reference_status: str | None = None,
        quality: str | None = None,
        style_version: str | None = None,
    ) -> Path:
        self.ensure_dirs()
        path = self.reference_vault_dir / f"{reference_id}.md"
        if not path.exists():
            raise FileNotFoundError(f"Reference not found: {reference_id}")

        text = path.read_text(encoding="utf-8")
        metadata, body = self._split_frontmatter(text)
        if reference_status is not None:
            normalized_status = reference_status.strip().lower()
            if normalized_status not in REFERENCE_ALL_STATUSES:
                raise ValueError(f"reference_status must be one of: {sorted(REFERENCE_ALL_STATUSES)}")
            metadata["reference_status"] = normalized_status
            metadata["статус"] = normalized_status in REFERENCE_ACTIVE_STATUSES
        if quality is not None:
            normalized_quality = quality.strip().lower()
            if normalized_quality not in REFERENCE_QUALITY_SCORES:
                raise ValueError(f"quality must be one of: {sorted(REFERENCE_QUALITY_SCORES)}")
            metadata["quality"] = normalized_quality
        if style_version is not None:
            metadata["style_version"] = style_version.strip() or None
        metadata["updated_at"] = now_moscow_iso()[:7]

        updated = self._render_markdown_with_frontmatter(metadata, body)
        path.write_text(updated, encoding="utf-8")
        legacy_path = self.references_dir / f"{reference_id}.md"
        if legacy_path.exists():
            legacy_path.write_text(updated, encoding="utf-8")
        if AUTO_REINDEX_REFERENCES:
            self.reindex_reference_vault_best_effort()
        return path

    # Назначение: грубо проверить текст на прямые идентификаторы перед reference promotion.
    # Вход: полный markdown reference.
    # Выход: ничего; при подозрительном паттерне бросает ValueError.
    def assert_no_direct_identifiers(self, text: str) -> None:
        for pattern in DIRECT_IDENTIFIER_PATTERNS:
            match = pattern.search(text)
            if match:
                raise ValueError(f"Potential PHI/direct identifier detected: {match.group(0)!r}")

    # Назначение: проверить, что metadata для reference не содержит запрещенных YAML ключей.
    # Вход: CaseMetadata.
    # Выход: ничего; при запрещенном ключе бросает ValueError.
    def ensure_reference_frontmatter_safe(self, metadata: CaseMetadata) -> None:
        forbidden = FORBIDDEN_REFERENCE_KEYS.intersection(metadata.extra.keys())
        if forbidden:
            raise ValueError(f"Forbidden reference metadata keys: {sorted(forbidden)}")

    # Назначение: собрать markdown из metadata и body без изменения тела.
    # Вход: YAML metadata dict и markdown body без внешних --- delimiters.
    # Выход: полный markdown-файл с frontmatter.
    def _render_markdown_with_frontmatter(self, metadata: dict[str, Any], body: str) -> str:
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return "\n".join(["---", frontmatter, "---", "", body.strip(), ""])

    # Назначение: записать CaseRecord в markdown с YAML frontmatter и секциями.
    # Вход: record и путь назначения.
    # Выход: путь к записанному файлу.
    def _write_case(self, record: CaseRecord, path: Path) -> Path:
        self.ensure_dirs()
        frontmatter = yaml.safe_dump(
            record.metadata.to_frontmatter(),
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        body = [
            "---",
            frontmatter,
            "---",
            "",
            CASE_SECTIONS["input"],
            "",
            record.input_text.strip(),
            "",
            CASE_SECTIONS["assistant_draft"],
            "",
            record.assistant_draft.strip(),
            "",
            CASE_SECTIONS["roman_final"],
            "",
            record.roman_final.strip(),
            "",
            CASE_SECTIONS["feedback"],
            "",
            *[f"- {item}" for item in record.feedback],
            "",
            CASE_SECTIONS["error_tags"],
            "",
            *[f"- {tag}" for tag in record.error_tags],
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(body), encoding="utf-8")
        return path

    # Назначение: прочитать CaseRecord из markdown case-файла.
    # Вход: путь к файлу.
    # Выход: CaseRecord.
    def _read_case(self, path: Path) -> CaseRecord:
        text = path.read_text(encoding="utf-8")
        metadata_dict, body = self._split_frontmatter(text)
        sections = self._split_case_sections(body)
        return CaseRecord(
            metadata=CaseMetadata.from_frontmatter(metadata_dict),
            input_text=sections.get("input", ""),
            assistant_draft=sections.get("assistant_draft", ""),
            roman_final=sections.get("roman_final", ""),
            feedback=self._parse_bullets(sections.get("feedback", "")),
            error_tags=self._parse_bullets(sections.get("error_tags", "")),
        )

    # Назначение: отделить YAML frontmatter от markdown body.
    # Вход: полный текст case-файла.
    # Выход: (metadata dict, body text).
    def _split_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        if not text.startswith("---\n"):
            raise ValueError("Case file has no YAML frontmatter")
        _, yaml_text, body = text.split("---", 2)
        metadata = yaml.safe_load(yaml_text) or {}
        return metadata, body.strip()

    # Назначение: разделить body case-файла на именованные секции.
    # Вход: markdown body.
    # Выход: dict: input/assistant_draft/roman_final/feedback/error_tags.
    def _split_case_sections(self, body: str) -> dict[str, str]:
        marker_to_key = {marker: key for key, marker in CASE_SECTIONS.items()}
        pattern = re.compile(r"^## (Input|Assistant draft|Roman final|Feedback|Error tags)[ \t]*$", re.MULTILINE)
        matches = list(pattern.finditer(body))
        sections: dict[str, str] = {}
        for index, match in enumerate(matches):
            marker = match.group(0)
            key = marker_to_key[marker]
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            sections[key] = body[start:end].strip()
        return sections

    # Назначение: превратить markdown bullets в список строк.
    # Вход: текст вида "- пункт 1\n- пункт 2".
    # Выход: ["пункт 1", "пункт 2"].
    def _parse_bullets(self, text: str) -> list[str]:
        items: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                items.append(line[2:].strip())
            elif line:
                items.append(line)
        return items

    # Назначение: оставить историю case в предыдущих статусных папках.
    # Вход: case_id и актуальная папка; аргументы сохранены для совместимости вызовов.
    # Выход: ничего не удаляется — это безопаснее для аудита learning loop.
    def _remove_other_case_copies(self, case_id: str, keep_dir: Path) -> None:
        return None

    # Назначение: выделить из финального протокола ту часть, которая должна стать
    # целевым блоком reference.
    # Вход: CaseRecord; roman_final может быть как только заключением, так и полным
    # протоколом с маркерами "Заключение:" / "Рекомендации:".
    # Выход: (заключение_или_финальный_текст, рекомендации). Для task=conclusion
    # сохраняем только собственно заключение, иначе few-shot загрязняется полным
    # протоколом внутри блока "Заключение".
    def _reference_target_text(self, record: CaseRecord) -> tuple[str, str]:
        final_text = record.roman_final.strip()
        if record.metadata.task != "conclusion" or not final_text:
            return final_text, ""

        conclusion_pattern = re.compile(r"^(?:##\s*)?Заключение\s*:\s*$", re.IGNORECASE | re.MULTILINE)
        conclusion_matches = list(conclusion_pattern.finditer(final_text))
        if not conclusion_matches:
            return final_text, ""

        # Если Роман прислал полный протокол, в тексте может быть несколько
        # маркеров "Заключение:". Для reference нужен последний — финальный.
        conclusion_start = conclusion_matches[-1].end()
        conclusion_block = final_text[conclusion_start:].strip()

        recommendation_pattern = re.compile(
            r"^(?:##\s*)?(?:Рекомендовано|Рекомендации)\s*:\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        recommendation_match = recommendation_pattern.search(conclusion_block)
        if recommendation_match:
            conclusion = conclusion_block[: recommendation_match.start()].strip()
            recommendation = conclusion_block[recommendation_match.end() :].strip()
            return conclusion, recommendation
        return conclusion_block, ""

    # Назначение: разделить roman_final для combined task на description и conclusion.
    # Вход: CaseRecord и предварительно извлечённый target_text.
    # Выход: (description, conclusion) — раздельные блоки.
    def _split_combined_target(self, record: CaseRecord, target_text: str) -> tuple[str, str]:
        """
        Для task=description_and_conclusion: разделить финальный текст на
        описание и заключение по маркеру 'Заключение:'.
        """
        text = target_text.strip()
        if not text:
            return "", ""

        conclusion_pattern = re.compile(
            r"^(?:##\s*)?Заключение\s*:\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        match = conclusion_pattern.search(text)
        if match:
            description = text[:match.start()].strip()
            conclusion = text[match.end():].strip()
            return description, conclusion

        # Если маркер не найден — весь текст как description, conclusion пусто
        return text, ""

    # Назначение: собрать markdown reference из финального case.
    # Вход: CaseRecord.
    # Выход: текст reference-файла в task-aware v2 schema.
    #
    # Для task=conclusion: source=описание, target=заключение
    # Для task=description: source=диктовка, target=описание
    # Для task=description_and_conclusion: source=диктовка, target=описание+заключение
    def _render_reference(
        self,
        record: CaseRecord,
        reference_status: str = "candidate",
        quality: str = "standard",
        style_version: str | None = None,
    ) -> str:
        normalized_status = reference_status.strip().lower()
        normalized_quality = quality.strip().lower()
        if normalized_status not in REFERENCE_ALL_STATUSES:
            raise ValueError(f"reference_status must be one of: {sorted(REFERENCE_ALL_STATUSES)}")
        if normalized_quality not in REFERENCE_QUALITY_SCORES:
            raise ValueError(f"quality must be one of: {sorted(REFERENCE_QUALITY_SCORES)}")
        now = now_moscow_iso()
        task = record.metadata.task
        target_text, recommendation = self._reference_target_text(record)

        # V2 task-aware schema
        from .reference_schema import render_v2_reference, SCHEMA_VERSION_V2

        metadata = {
            "анамнез": record.metadata.clinical_context or None,
            "область": record.metadata.area,
            "сравнение": record.metadata.comparison,
            "экстренность": False,
            "статус": normalized_status in REFERENCE_ACTIVE_STATUSES,
            "задача": task,
            "reference_status": normalized_status,
            "quality": normalized_quality,
            "style_version": style_version or now[:7],
            "created_at": now[:7],
            "updated_at": now[:7],
        }

        # Определяем target блоки по task
        target_description = ""
        target_conclusion = ""
        target_recommendations = recommendation.strip()

        if task == "conclusion":
            # source = описание (input_text), target = заключение
            target_conclusion = target_text.strip()
        elif task == "description":
            # source = диктовка (input_text), target = описание
            target_description = target_text.strip()
        elif task == "description_and_conclusion":
            # source = диктовка, target = описание + заключение
            # roman_final может содержать оба блока
            target_description, target_conclusion = self._split_combined_target(record, target_text)
        else:
            # edit_description / edit_conclusion / fallback → legacy behavior
            target_conclusion = target_text.strip()

        return render_v2_reference(
            task=task,
            areas=record.metadata.area,
            source_input=record.input_text.strip(),
            target_description=target_description,
            target_conclusion=target_conclusion,
            target_recommendations=target_recommendations,
            metadata=metadata,
        )
