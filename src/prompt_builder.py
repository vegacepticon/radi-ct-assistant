"""
Сборка промпта с few-shot примерами.
"""
from .retriever import RetrievalResult
from .config import PROMPTS_DIR


def load_system_prompt(mode: str = "fast") -> str:
    """Загружает системный промпт из файла."""
    filename = f"system_{mode}.txt"
    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
        # Fallback — базовый промпт
        return _default_system_prompt(mode)
    return filepath.read_text(encoding="utf-8")


def _default_system_prompt(mode: str) -> str:
    """Дефолтный системный промпт (если файл не найден)."""
    base = (
        "Ты — врач-радиолог. Твоя задача — сформировать заключение КТ "
        "на основе описательной части исследования.\n\n"
        "Следуй структуре и стилю примеров точно.\n"
        "Группируй находки по патологиям, не по органам.\n"
        "Используй те же формулировки и уровень детализации, что в примерах.\n"
        "Не добавляй находки, которых нет в описании.\n"
        "Не пропускай важные находки из описания.\n"
    )
    if mode == "analytical":
        base += (
            "\nЕсли находки неоднозначны и допускают несколько интерпретаций, "
            "построить дифференциальный ряд после основного заключения.\n"
            "Диффдиагноз оформить отдельным блоком после разделителя '---'.\n"
            "Для каждого пункта дать обоснование (почему возможно / "
            "почему менее вероятно).\n"
        )
    return base


def build_prompt(
    description: str,
    references: list[RetrievalResult],
    mode: str = "fast",
    clinical_context: str = "",
) -> str:
    """
    Собирает полный промпт: system + few-shot + input.
    """
    system = load_system_prompt(mode)

    # Few-shot примеры
    few_shot_parts = []
    for i, ref in enumerate(references, 1):
        block = f"--- Пример {i} ---\n"
        block += f"Описание:\n{ref.description}\n\n"
        block += f"Заключение:\n{ref.conclusion}"
        if ref.recommendation:
            block += f"\n\nРекомендовано:\n{ref.recommendation}"
        few_shot_parts.append(block)

    few_shot = "\n\n".join(few_shot_parts)

    # Входные данные
    input_parts = []
    if clinical_context:
        input_parts.append(f"Клинический контекст: {clinical_context}")
    input_parts.append(f"Описание:\n{description}")

    input_block = "\n\n".join(input_parts)

    # Финальная сборка
    prompt = f"{system}\n\n{few_shot}\n\n--- Текущее исследование ---\n{input_block}\n\nЗаключение:"

    return prompt