"""
Task-aware сборка промпта с few-shot примерами.

Phase 3: разделение на task-specific renderers.
- build_conclusion_prompt() — только заключение
- build_description_prompt() — только описание
- build_description_and_conclusion_prompt() — описание + заключение

Каждый renderer загружает свой task-specific prompt file и
использует правильный output contract.
"""
from typing import TYPE_CHECKING

from .config import PROMPTS_DIR
from .reference_schema import TaskAwareReference, parse_reference

if TYPE_CHECKING:
    from .retriever import RetrievalResult


# --- System prompt loading ---

def load_system_prompt(mode: str = "fast") -> str:
    """Загружает базовый системный промпт из файла."""
    filename = f"system_{mode}.txt"
    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
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


def _load_task_prompt(task: str) -> str:
    """
    Загрузить task-specific prompt file.

    Файлы:
    - prompts/conclusion_from_description.txt
    - prompts/description_from_notes.txt
    - prompts/description_and_conclusion_from_notes.txt
    """
    task_files = {
        "conclusion": "conclusion_from_description.txt",
        "description": "description_from_notes.txt",
        "description_and_conclusion": "description_and_conclusion_from_notes.txt",
        "edit_description": "description_from_notes.txt",
        "edit_conclusion": "conclusion_from_description.txt",
    }
    filename = task_files.get(task, "conclusion_from_description.txt")
    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
        return ""
    return filepath.read_text(encoding="utf-8").strip()


# --- Self-check checklist ---

SELF_CHECK = """
## Self-check (перед финальным ответом)

- [ ] Факты не добавлены (нет выдуманных находок)
- [ ] Размеры/сторона/сегмент/уровень сохранены точно
- [ ] Клинически значимые находки не потеряны
- [ ] Структура соответствует task
- [ ] Нет запрещённых формулировок: «без существенной динамики», «без убедительной динамики», «выраженный/значительный/резкий»
- [ ] Нет буквы «ё»
- [ ] Нет модality prefix («КТ-признаки», «РКТ-признаки») если модальность очевидна
"""


# --- Few-shot serialization ---

def _serialize_reference_for_task(ref: "RetrievalResult", task: str) -> str:
    """
    Сериализовать retrieved reference для few-shot блока.

    Для v1 references: использует legacy формат Описание → Заключение.
    Для v2 references: использует task-specific блоки.
    """
    # Пытаемся загрузить через reference_schema для v2
    ref_path = getattr(ref, "filepath", "")
    if ref_path:
        try:
            parsed = parse_reference(ref_path)
            if parsed and parsed.is_v2:
                return parsed.few_shot_block()
        except Exception:
            pass

    # Fallback: v1 legacy формат через RetrievalResult fields
    block = f"Описание:\n{ref.description}\n\n"
    block += f"Заключение:\n{ref.conclusion}"
    if ref.recommendation:
        block += f"\n\nРекомендовано:\n{ref.recommendation}"
    return block


# --- Task-specific renderers ---

def build_conclusion_prompt(
    description: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
) -> str:
    """
    Prompt для task=conclusion: готовое описание → заключение.

    Output contract: только заключение, без описания.
    """
    system = load_system_prompt(mode)
    task_rules = _load_task_prompt("conclusion")

    # Few-shot примеры
    few_shot_parts = []
    for i, ref in enumerate(references, 1):
        block = f"--- Пример {i} ---\n"
        block += _serialize_reference_for_task(ref, "conclusion")
        few_shot_parts.append(block)

    few_shot = "\n\n".join(few_shot_parts)

    # Входные данные
    input_parts = []
    if clinical_context:
        input_parts.append(f"Клинический контекст: {clinical_context}")
    input_parts.append(f"Описание:\n{description}")
    input_block = "\n\n".join(input_parts)

    # Output contract
    output_contract = "Заключение:"

    prompt = f"{system}\n\n{task_rules}\n\n{few_shot}\n\n--- Текущее исследование ---\n{input_block}\n\n{output_contract}"
    return prompt


def build_description_prompt(
    input_text: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
) -> str:
    """
    Prompt для task=description: диктовка/находки → структурированное описание.

    Output contract: только описание, БЕЗ заключения.
    """
    system = load_system_prompt(mode)
    task_rules = _load_task_prompt("description")

    # Few-shot примеры
    few_shot_parts = []
    for i, ref in enumerate(references, 1):
        block = f"--- Пример {i} ---\n"
        block += _serialize_reference_for_task(ref, "description")
        few_shot_parts.append(block)

    few_shot = "\n\n".join(few_shot_parts)

    # Входные данные
    input_parts = []
    if clinical_context:
        input_parts.append(f"Клинический контекст: {clinical_context}")
    input_parts.append(f"Входные данные (диктовка/находки):\n{input_text}")
    input_block = "\n\n".join(input_parts)

    # Output contract — описание, НЕ заключение
    output_contract = "Описание:"

    prompt = f"{system}\n\n{task_rules}\n\n{few_shot}\n\n--- Текущее исследование ---\n{input_block}\n\n{output_contract}"
    return prompt


def build_description_and_conclusion_prompt(
    input_text: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
) -> str:
    """
    Prompt для task=description_and_conclusion: диктовка → описание + заключение.

    Output contract: сначала описание, затем заключение.
    """
    system = load_system_prompt(mode)
    task_rules = _load_task_prompt("description_and_conclusion")

    # Few-shot примеры
    few_shot_parts = []
    for i, ref in enumerate(references, 1):
        block = f"--- Пример {i} ---\n"
        block += _serialize_reference_for_task(ref, "description_and_conclusion")
        few_shot_parts.append(block)

    few_shot = "\n\n".join(few_shot_parts)

    # Входные данные
    input_parts = []
    if clinical_context:
        input_parts.append(f"Клинический контекст: {clinical_context}")
    input_parts.append(f"Входные данные (диктовка/находки):\n{input_text}")
    input_block = "\n\n".join(input_parts)

    # Output contract — описание + заключение
    output_contract = "Описание:\n[сформируй структурированное описание]\n\nЗаключение:\n[сформируй заключение на основе описания]"

    prompt = f"{system}\n\n{task_rules}\n\n{few_shot}\n\n--- Текущее исследование ---\n{input_block}\n\n{output_contract}"
    return prompt


# --- Main entry point ---

def build_prompt(
    description: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
    task: str = "conclusion",
) -> str:
    """
    Task-aware сборка промпта.

    Выбирает renderer на основе task:
    - conclusion → build_conclusion_prompt
    - description → build_description_prompt
    - description_and_conclusion → build_description_and_conclusion_prompt
    """
    if task == "description":
        return build_description_prompt(
            description, references, mode=mode, clinical_context=clinical_context
        )
    elif task == "description_and_conclusion":
        return build_description_and_conclusion_prompt(
            description, references, mode=mode, clinical_context=clinical_context
        )
    else:
        # conclusion (default) и edit_* используют conclusion renderer
        return build_conclusion_prompt(
            description, references, mode=mode, clinical_context=clinical_context
        )