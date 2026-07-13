"""
Локальный аудит-журнал RadiCT событий без медицинского текста.

Зачем нужен этот модуль:
- Записывает структурные события workflow в data/audit/radi_ct_events.jsonl.
- Не записывает описание, заключение, анамнез, PHI, идентификаторы пациентов,
  prompt или ненужные полные пути.
- Позволяет diagnose "тихие сбои" — когда workflow не был завершён.

Безопасность:
- Только case_id, session_id, event type, rag_status, reason_code.
- Никакого медицинского текста.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BASE_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """Файловый JSONL аудит-журнал RadiCT событий."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or BASE_DIR
        self.audit_dir = self.base_dir / "data" / "audit"
        self.audit_file = self.audit_dir / "radi_ct_events.jsonl"

    def _ensure(self) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def log_event(
        self,
        event: str,
        case_id: str = "",
        session_id: str = "",
        task: str = "",
        rag_status: str = "",
        reference_count: int = 0,
        reference_id: str = "",
        reason_code: str = "",
    ) -> None:
        """Записать событие в аудит-журнал.

        Аргументы:
        - event: тип события (workflow_started, rag_completed, draft_created,
          awaiting_feedback, accepted, corrected, reference_saved,
          index_updated, capture_failed, capture_pending)
        - case_id: идентификатор case (не медицинский текст)
        - session_id: Hermes session ID
        - task: conclusion / description / description_and_conclusion
        - rag_status: used / no_hits / unavailable / error
        - reference_count: количество найденных references
        - reference_id: идентификатор сохранённого reference
        - reason_code: причина пропуска или pending
        """
        self._ensure()
        record: dict[str, Any] = {
            "event": event,
            "timestamp": _now_iso(),
        }
        # Только структурные метаданные, никакого медицинского текста
        if case_id:
            record["case_id"] = case_id
        if session_id:
            record["session_id"] = session_id
        if task:
            record["task"] = task
        if rag_status:
            record["rag_status"] = rag_status
        if reference_count:
            record["reference_count"] = reference_count
        if reference_id:
            record["reference_id"] = reference_id
        if reason_code:
            record["reason_code"] = reason_code

        with self.audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Прочитать последние N событий."""
        self._ensure()
        if not self.audit_file.exists():
            return []
        lines = self.audit_file.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return events