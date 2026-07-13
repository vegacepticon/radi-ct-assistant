"""
Task-aware сборка промпта с few-shot примерами.

Phase 3: разделение на task-specific renderers.
Phase 4: area templates + output_mode (full_systematic / findings_only).

- build_conclusion_prompt() — только заключение
- build_description_prompt() — только описание
- build_description_and_conclusion_prompt() — описание + заключение

Каждый renderer загружает свой task-specific prompt file, подключает
area template и использует правильный output contract.
"""
from typing import TYPE_CHECKING

from pathlib import Path

from .config import PROMPTS_DIR
from .reference_schema import TaskAwareReference, parse_reference

if TYPE_CHECKING:
    from .retriever import RetrievalResult


# --- Area template loading ---

# Mapping from area names to template files
AREA_TEMPLATE_MAP = {
    "ОГК": "chest_ct.md",
    "грудная клетка": "chest_ct.md",
    "органы грудной клетки": "chest_ct.md",
    "ГМ": "head_ct.md",
    "головной мозг": "head_ct.md",
    "голова": "head_ct.md",
    "ОБП": "abdomen_ct.md",
    "органы брюшной полости": "abdomen_ct.md",
    "брюшная полость": "abdomen_ct.md",
    "ОМТ": "pelvis_ct.md",
    "органы малого таза": "pelvis_ct.md",
    "малый таз": "pelvis_ct.md",
    "Шейный отдел позвоночника": "cervical_spine_ct.md",
    "шейный отдел": "cervical_spine_ct.md",
    "шейный отдел позвоночника": "cervical_spine_ct.md",
    "Грудной отдел позвоночника": "thoracic_lumbar_spine_ct.md",
    "грудной отдел позвоночника": "thoracic_lumbar_spine_ct.md",
    "Поясничный отдел позвоночника": "thoracic_lumbar_spine_ct.md",
    "поясничный отдел позвоночника": "thoracic_lumbar_spine_ct.md",
    "поясничный отдел": "thoracic_lumbar_spine_ct.md",
    "грудопоясничный отдел": "thoracic_lumbar_spine_ct.md",
}

# Template directory
TEMPLATES_DIR = PROMPTS_DIR / "templates"


def load_area_template(area: str) -> str:
    """
    Загрузить area template по имени области.
    Возвращает пустую строку, если template не найден.
    """
    if not area:
        return ""
    normalized = area.strip()
    filename = AREA_TEMPLATE_MAP.get(normalized)
    if not filename:
        # Попробовать case-insensitive
        for key, val in AREA_TEMPLATE_MAP.items():
            if key.lower() == normalized.lower():
                filename = val
                break
    if not filename:
        return ""
    filepath = TEMPLATES_DIR / filename
    if not filepath.exists():
        return ""
    return filepath.read_text(encoding="utf-8").strip()


def load_area_template_for_areas(areas: list[str]) -> str:
    """
    Загрузить area template для списка областей.
    Если несколько областей — ищем combined template или берём первый найденный.
    """
    if not areas:
        return ""

    # Проверяем комбинацию ОГК+ОБП+ОМТ
    area_set = {a.strip() for a in areas}
    if {"ОГК", "ОБП", "ОМТ"}.issubset(area_set):
        filepath = TEMPLATES_DIR / "combined_chest_abdomen_pelvis.md"
        if filepath.exists():
            return filepath.read_text(encoding="utf-8").strip()

    # Иначе берём первый распознанный
    for area in areas:
        template = load_area_template(area)
        if template:
            return template
    return ""


def _format_template_for_prompt(template: str, output_mode: str = "full_systematic") -> str:
    """
    Извлечь из template markdown body (без заголовка) и
    адаптировать для prompt inclusion.
    """
    if not template:
        return ""
    # Убираем markdown заголовки первого уровня (# Template: ...)
    lines = template.splitlines()
    # Найдём строку "## Full systematic template" или "## Section order"
    # и возьмём всё что после "## Full systematic template" блока
    in_template = False
    template_lines = []
    for line in lines:
        if line.startswith("## Full systematic template"):
            in_template = True
            continue
        if line.startswith("## Notes"):
            break
        if in_template:
            template_lines.append(line)

    if template_lines:
        result = "\n".join(template_lines).strip()
        # Если findings_only — добавляем предупреждение
        if output_mode == "findings_only":
            result = (
                "РЕЖИМ: Только структурирование предоставленных находок.\n"
                "Запрещено достраивать нормальные структуры, которых не было во входе.\n"
                "Опиши только то, что передано, в правильном порядке секций.\n\n"
                "Canonical section order (для референса):\n"
                + result
            )
        return result
    return ""


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
        "finding_description": "finding_description.txt",
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

    # Fallback для результатов без доступного v2 filepath.
    # В conclusion-сценарии source — готовое описание, поэтому сохраняем
    # привычную few-shot пару «Описание → Заключение». В description-семействе
    # source — диктовка/черновые данные, а target — готовое описание.
    if task in {"description", "finding_description", "edit_description"}:
        block = f"Исходный текст:\n{ref.description}\n\n"
        block += f"Описание:\n{ref.conclusion}"
    else:
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
    areas: list[str] | None = None,
    output_mode: str = "full_systematic",
) -> str:
    """
    Prompt для task=conclusion: готовое описание → заключение.

    Output contract: только заключение, без описания.
    Area template не подключается — для conclusion нужен только input + examples.
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
    areas: list[str] | None = None,
    output_mode: str = "full_systematic",
) -> str:
    """
    Prompt для task=description: диктовка/находки → структурированное описание.

    Output contract: только описание, БЕЗ заключения.
    Area template подключается для определения canonical section order.
    output_mode: full_systematic (достраивать норму) или findings_only (только переданное).
    """
    system = load_system_prompt(mode)
    task_rules = _load_task_prompt("description")

    # Area template
    area_template = ""
    if areas:
        raw_template = load_area_template_for_areas(areas)
        area_template = _format_template_for_prompt(raw_template, output_mode)

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

    # Сборка prompt с area template
    parts = [system, task_rules]
    if area_template:
        parts.append(f"## Area template\n\n{area_template}")
    parts.append(few_shot)
    parts.append(f"--- Текущее исследование ---\n{input_block}")
    parts.append(output_contract)

    return "\n\n".join(parts)


def build_finding_description_prompt(
    input_text: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
    areas: list[str] | None = None,
    output_mode: str = "findings_only",
) -> str:
    """Prompt для одной находки: диктовка → одна готовая формулировка.

    Намеренно не подключает full-systematic area template и системный prompt
    для заключения. При критическом дефиците данных допускается только
    короткий блок уточняющих вопросов.
    """
    task_rules = _load_task_prompt("finding_description")

    few_shot_parts = []
    for i, ref in enumerate(references, 1):
        block = f"--- Пример {i} ---\n"
        block += _serialize_reference_for_task(ref, "finding_description")
        few_shot_parts.append(block)

    input_parts = []
    if clinical_context:
        input_parts.append(f"Клинический контекст: {clinical_context}")
    if areas:
        input_parts.append(f"Область: {', '.join(areas)}")
    input_parts.append(f"Неструктурированная диктовка одной находки:\n{input_text}")

    parts = [task_rules]
    if few_shot_parts:
        parts.append("\n\n".join(few_shot_parts))
    parts.append("--- Текущая находка ---\n" + "\n\n".join(input_parts))
    parts.append(
        "Контракт ответа: только готовая формулировка одной находки без заголовка; "
        "если критически не хватает данных — только «Уточняющие вопросы:» "
        "и не более трех вопросов."
    )
    return "\n\n".join(parts)


def build_description_and_conclusion_prompt(
    input_text: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
    areas: list[str] | None = None,
    output_mode: str = "full_systematic",
) -> str:
    """
    Prompt для task=description_and_conclusion: диктовка → описание + заключение.

    Output contract: сначала описание, затем заключение.
    Area template подключается для description.
    """
    system = load_system_prompt(mode)
    task_rules = _load_task_prompt("description_and_conclusion")

    # Area template
    area_template = ""
    if areas:
        raw_template = load_area_template_for_areas(areas)
        area_template = _format_template_for_prompt(raw_template, output_mode)

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

    # Сборка prompt с area template
    parts = [system, task_rules]
    if area_template:
        parts.append(f"## Area template\n\n{area_template}")
    parts.append(few_shot)
    parts.append(f"--- Текущее исследование ---\n{input_block}")
    parts.append(output_contract)

    return "\n\n".join(parts)


# --- Main entry point ---

def build_prompt(
    description: str,
    references: list["RetrievalResult"],
    mode: str = "fast",
    clinical_context: str = "",
    task: str = "conclusion",
    areas: list[str] | None = None,
    output_mode: str = "full_systematic",
) -> str:
    """
    Task-aware сборка промпта.

    Выбирает renderer на основе task:
    - conclusion → build_conclusion_prompt
    - description → build_description_prompt
    - description_and_conclusion → build_description_and_conclusion_prompt

    areas и output_mode передаются в description/combined renderers
    для подключения area template и выбора режима описания.
    """
    if task == "description":
        return build_description_prompt(
            description, references, mode=mode, clinical_context=clinical_context,
            areas=areas, output_mode=output_mode,
        )
    elif task == "finding_description":
        return build_finding_description_prompt(
            description, references, mode=mode, clinical_context=clinical_context,
            areas=areas, output_mode="findings_only",
        )
    elif task == "description_and_conclusion":
        return build_description_and_conclusion_prompt(
            description, references, mode=mode, clinical_context=clinical_context,
            areas=areas, output_mode=output_mode,
        )
    else:
        # conclusion (default) и edit_* используют conclusion renderer
        return build_conclusion_prompt(
            description, references, mode=mode, clinical_context=clinical_context,
        )