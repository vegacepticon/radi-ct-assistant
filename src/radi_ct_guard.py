"""
RadiCT workflow guard — fail-visible инвариантная проверка.

Зачем нужен этот модуль:
- Проверяет, что завершённый RadiCT-кейс имеет один из трёх обязательных результатов:
  reference_id, skip_reason или capture_pending/error.
- Не маскирует тихий пропуск.
- Используется CLI audit-status --strict и может быть вызвана из плагина Hermes.

Безопасность:
- Не содержит медицинский текст.
- Только структурные метаданные.

Остаточный риск:
- Без плагина Hermes guard (transform_llm_output/pre_llm_call) проверка выполняется
  только когда агент сам вызывает CLI. Если агент просто отвечает текстом без вызова
  инструментов, guard не срабатывает.
- Плагин Hermes, который мог бы заменить тихий ответ на fail-visible сообщение,
  требует изменения Hermes config и не внедрён без подтверждения Романа.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class InvariantCheck:
    """Результат проверки инварианта завершённого RadiCT-кейса."""

    passed: bool
    outcome: str  # "reference_id" | "skip_reason" | "capture_pending" | "violation"
    reference_id: str = ""
    skip_reason: str = ""
    message: str = ""


def check_completion_invariant(
    reference_result: Any | None = None,
    skip_reason: str = "",
    capture_pending: bool = False,
    capture_pending_reason: str = "",
) -> InvariantCheck:
    """Проверить, что RadiCT-кейс завершён с одним из трёх обязательных результатов.

    Аргументы:
    - reference_result: PromotionResult | None. Если saved=True и reference_id непустой — success.
    - skip_reason: конкретная причина пропуска (PHI risk, ambiguous, explicit "не сохраняй").
    - capture_pending: True, если операция не завершена и сохранена для recovery.
    - capture_pending_reason: причина capture_pending.

    Возвращает InvariantCheck с passed=True, если инвариант соблюдён.
    """
    # A: reference_id + сохранено
    if reference_result is not None and getattr(reference_result, "saved", False):
        ref_id = getattr(reference_result, "reference_id", "")
        if ref_id:
            return InvariantCheck(
                passed=True,
                outcome="reference_id",
                reference_id=ref_id,
                message=f"Reference saved: {ref_id}",
            )

    # B: skip_reason
    if skip_reason:
        return InvariantCheck(
            passed=True,
            outcome="skip_reason",
            skip_reason=skip_reason,
            message=f"Skipped: {skip_reason}",
        )

    # C: capture_pending/error
    if capture_pending:
        return InvariantCheck(
            passed=True,
            outcome="capture_pending",
            skip_reason=capture_pending_reason or "pending",
            message=f"Capture pending: {capture_pending_reason or 'ambiguous'}",
        )

    # Violation: тихий ответ без обязательного результата
    return InvariantCheck(
        passed=False,
        outcome="violation",
        message=(
            "RadiCT workflow violation: no reference_id, skip_reason, or capture_pending. "
            "The session ended without a verified outcome. This must not happen silently."
        ),
    )


FAIL_VISIBLE_MESSAGE = (
    "⚠️ RadiCT workflow не завершен: case/reference operation не выполнена "
    "или не подтверждена. Состояние сохранено как capture_pending; "
    "требуется повторная обработка на следующем сообщении."
)