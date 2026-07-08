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
    re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b"),  # точные даты dd.mm.yyyy
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # точные даты yyyy-mm-dd
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
        return record

    # Назначение: сохранить case в папку drafts.
    # Вход: CaseRecord со status=draft.
    # Выход: путь к markdown-файлу.
    def save_draft(self, record: CaseRecord) -> Path:
        record.metadata.status = "draft"
        return self._write_case(record, self.drafts_dir / f"{record.metadata.case_id}.md")

    # Назначение: принять черновик без правок и перенести case в accepted.
    # Вход: case_id; опционально save_as_reference=True для promotion.
    # Выход: обновленный CaseRecord.
    def accept_case(self, case_id: str, save_as_reference: bool = False) -> CaseRecord:
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
        if save_as_reference:
            self.promote_to_reference(case_id)
        return record

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
    ) -> CaseRecord:
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
        if save_as_reference:
            self.promote_to_reference(case_id)
        return record

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
    def create_lesson_candidate(self, record: CaseRecord) -> Path:
        self.ensure_dirs()
        path = self.lesson_candidates_dir / f"{record.metadata.case_id}.md"
        lines = [
            f"# Lesson candidate {record.metadata.case_id}",
            "",
            f"Task: {record.metadata.task}",
            f"Area: {', '.join(record.metadata.area) if record.metadata.area else '-'}",
            "",
            "## Feedback",
            *[f"- {item}" for item in record.feedback],
            "",
            "## Error tags",
            *[f"- {tag}" for tag in record.error_tags],
        ]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    # Назначение: перенести accepted/corrected case в reference base для few-shot.
    # Вход: case_id уже принятого или исправленного case.
    # Выход: путь к reference markdown-файлу.
    def promote_to_reference(self, case_id: str) -> Path:
        record = self.load_case(case_id)
        if record.metadata.status not in ("accepted", "corrected"):
            raise ValueError("Only accepted/corrected cases can be promoted to references")
        if not record.roman_final.strip():
            raise ValueError("Cannot promote case without Roman final text")

        reference_text = self._render_reference(record)
        self.assert_no_direct_identifiers(reference_text)
        self.ensure_reference_frontmatter_safe(record.metadata)

        path = self.reference_vault_dir / f"{case_id}.md"
        path.write_text(reference_text, encoding="utf-8")

        # Legacy mirror: старый Chroma/parse_directory path оставляем рабочим,
        # но основным источником RAG становится Obsidian-like reference vault.
        legacy_path = self.references_dir / f"{case_id}.md"
        legacy_path.write_text(reference_text, encoding="utf-8")

        if AUTO_REINDEX_REFERENCES:
            self.reindex_reference_vault_best_effort()

        self.append_feedback_event(record, promoted_to_reference=True, promoted_to_skill=False)
        return path

    # Назначение: best-effort обновить OHS index после сохранения нового reference.
    # Вход: текущий reference_vault_dir.
    # Выход: ничего; ошибки не ломают accept/correct, потому что reference уже сохранён.
    def reindex_reference_vault_best_effort(self) -> None:
        try:
            from .ohs import ohs_reindex

            ohs_reindex(vault_dir=self.reference_vault_dir, force=False)
        except Exception as e:
            # Promotion не должен падать только из-за временной недоступности OHS.
            # Пользователь увидит readiness через /api/rag/status или сможет
            # вручную вызвать /api/reindex после исправления окружения.
            print(f"[feedback_store] OHS reindex skipped/failed: {e}")

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

    # Назначение: собрать markdown reference из финального case.
    # Вход: CaseRecord.
    # Выход: текст reference-файла с минимальным безопасным YAML.
    def _render_reference(self, record: CaseRecord) -> str:
        metadata = {
            "анамнез": record.metadata.clinical_context or None,
            "область": record.metadata.area,
            "сравнение": record.metadata.comparison,
            "экстренность": False,
            "статус": True,
            "задача": record.metadata.task,
        }
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return "\n".join(
            [
                "---",
                frontmatter,
                "---",
                "",
                "Описание:",
                record.input_text.strip(),
                "",
                "Заключение:",
                record.roman_final.strip(),
                "",
            ]
        )
