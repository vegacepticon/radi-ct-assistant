"""
Session-safe хранилище связи Hermes session → active RadiCT case_id.

Зачем нужен этот модуль:
- Позволяет следующему сообщению «принимаю» или финальному протоколу
  однозначно связаться с существующим case без глобального current_case_id.
- Не содержит медицинский текст — только session_id, case_id, state, task, rag_status.
- Хранится в data/session_state.json внутри RADI_CT_BASE_DIR.
- Поддерживает несколько параллельных Telegram/Hermes-сессий без смешивания.

Безопасность:
- Файл не содержит описаний, заключений, анамнеза, PHI или идентификаторов пациентов.
- Только структурные метаданные для связывания сообщений.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class SessionStateStore:
    """Файловое JSON-хранилище session → case_id mapping."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or BASE_DIR
        self.state_dir = self.base_dir / "data"
        self.state_file = self.state_dir / "session_state.json"

    def _ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self.state_file.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        self._ensure()
        return json.loads(self.state_file.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict[str, Any]) -> None:
        self._ensure()
        self.state_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set_active_case(
        self,
        session_id: str,
        case_id: str,
        state: str = "awaiting_feedback",
        task: str = "conclusion",
        rag_status: str = "unknown",
    ) -> dict[str, Any]:
        """Связать session_id с активным case_id."""
        data = self._load()
        entry = {
            "case_id": case_id,
            "state": state,
            "task": task,
            "rag_status": rag_status,
            "updated_at": _now_iso(),
        }
        data[session_id] = entry
        self._save(data)
        return entry

    def get_active_case(self, session_id: str) -> dict[str, Any] | None:
        """Получить активный case_id для session_id, или None."""
        data = self._load()
        return data.get(session_id)

    def clear(self, session_id: str) -> None:
        """Удалить активный case для session_id."""
        data = self._load()
        data.pop(session_id, None)
        self._save(data)

    def list_active(self) -> dict[str, Any]:
        """Показать все активные session→case mappings."""
        return self._load()