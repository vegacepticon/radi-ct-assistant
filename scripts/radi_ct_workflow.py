#!/usr/bin/env python3
"""
Telegram/Hermes workflow-wrapper для RadiCT Assistant.

Зачем нужен этот файл:
    scripts/radi_ct_api.py — низкоуровневый HTTP CLI: он принимает точные
    команды и печатает JSON. Для Telegram/Hermes удобнее слой выше: человек
    отправляет сообщение в привычном формате, wrapper разбирает его, вызывает
    локальный RadiCT API и возвращает короткий Markdown-ответ для чата.

Базовая настройка:
    1. Запустить локальный API:
        uvicorn src.main:app --host 127.0.0.1 --port 8000

    2. Проверить доступность:
        python3 scripts/radi_ct_workflow.py health

    3. Сохранить draft из Telegram/Hermes-сообщения. В Hermes-only режиме
       сообщение должно содержать блок "Черновик ассистента:" или отдельный
       файл --assistant-draft:
        python3 scripts/radi_ct_workflow.py message message.md

    4. Сохранить исправление Романа:
        python3 scripts/radi_ct_workflow.py correct CASE_ID --final final.md --feedback feedback.md --tag incomplete_stable_findings_list

    5. Органично сохранить обычную консультационную сессию как corrected case + reference:
        python3 scripts/radi_ct_workflow.py capture-session session.md --tag style_refinement

    В Telegram/Hermes workflow accept/correct/capture-session по умолчанию также пытаются
    сохранить case в reference base. Backend выполняет PHI guard и отклоняет
    promotion, если находит прямые идентификаторы. Для редкого исключения есть
    флаг --no-save-as-reference.

Формат сообщения для команды `message`:

    РКТ заключение
    Область: ОГК
    Контекст: синтетический обезличенный пример
    Сравнение: да
    Режим: fast
    ---
    Описание: ...

Hermes должен подготовить черновик; wrapper только сохраняет case без вызова LLM:

    РКТ заключение
    Область: ОГК
    ---
    Описание: синтетическое описание.

    Черновик ассистента:
    Синтетическое заключение.

Формат `capture-session` для обычного диалога без заранее созданного case_id:

    РКТ заключение
    Область: ОГК
    Контекст: синтетический обезличенный пример
    ---
    Описание: синтетическое описание.

    Черновик ассистента:
    Синтетическое заключение.

    Финальный вариант:
    Финальное заключение Романа.

    Почему:
    - Краткое объяснение правок.

Команды пользователя, которые распознаёт wrapper:
    - РКТ заключение
    - РКТ описание
    - РКТ описание + заключение
    - Исправляю: / Почему: — для correction-сценария удобнее явная команда
      `correct`, потому что нужен case_id.

Безопасность:
    Wrapper сам не обезличивает текст. Он только передает текст в локальный API.
    Backend не отправляет данные во внешние LLM/API. Всё равно не сохраняйте
    реальные идентификаторы пациента в long-term reference base.
    В Telegram/Hermes workflow сохранение в reference base включено по
    умолчанию для accept/correct/capture-session, потому что это часть радиологического
    learning loop Романа. Backend дополнительно выполняет базовый PHI guard.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Когда файл запускают как `python3 scripts/radi_ct_workflow.py`, Python кладёт
# в sys.path папку scripts/, а не корень проекта. Поэтому импорт соседнего
# `scripts/radi_ct_api.py` через package-style `from scripts import ...` может
# упасть. Добавляем корень проекта явно: это сохраняет совместимость и с
# unit-тестами (`from scripts import radi_ct_workflow`), и с прямым CLI-запуском.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import radi_ct_api


TASK_BY_TRIGGER = {
    "ркт описание находки": "finding_description",
    "ркт находка": "finding_description",
    "ct finding description": "finding_description",
    "ркт заключение": "conclusion",
    "rct conclusion": "conclusion",
    "ct conclusion": "conclusion",
    "ркт описание": "description",
    "ct description": "description",
    "ркт описание + заключение": "description_and_conclusion",
    "ркт описание и заключение": "description_and_conclusion",
    "ct description + conclusion": "description_and_conclusion",
}

TRIGGER_ORDER = sorted(TASK_BY_TRIGGER, key=len, reverse=True)


@dataclass
class WorkflowMessage:
    """Структурированное представление Telegram/Hermes-сообщения."""

    task: str = "conclusion"
    input_type: str = "markdown"
    area: list[str] = field(default_factory=list)
    clinical_context: str = ""
    comparison: bool = False
    mode: str = "fast"
    output_mode: str = "full_systematic"
    input_text: str = ""
    assistant_draft: str = ""


# Назначение: прочитать UTF-8 текст из файла или stdin.
# Вход:
#   path — путь к файлу, None или "-".
#   Если path равен "-", читаем stdin; это удобно для Hermes pipe-сценариев.
# Выход: строка с исходным пользовательским сообщением.
def read_text(path: str | None) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


# Назначение: преобразовать русские/английские значения да/нет в bool.
# Вход: строка из поля "Сравнение: да" или похожего metadata-поля.
# Выход: True для да/yes/true/1, False для нет/no/false/0/пусто.
def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"да", "yes", "true", "1", "+", "есть", "имеется"}


# Назначение: разбить строку области исследования на список областей.
# Вход: "ОГК, ОБП" или "ОГК; ОБП".
# Выход: ["ОГК", "ОБП"]. Пустые элементы отбрасываются.
def parse_area(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


# Назначение: определить task по первой строке-сигналу.
# Вход: строка вроде "РКТ заключение" или "РКТ описание + заключение".
# Выход: внутреннее имя задачи API: conclusion / description / description_and_conclusion.
def task_from_trigger(line: str) -> str | None:
    normalized = line.strip().lower()
    for trigger in TRIGGER_ORDER:
        if normalized.startswith(trigger):
            return TASK_BY_TRIGGER[trigger]
    return None


# Назначение: отделить заголовочные metadata-строки от основного текста.
# Вход: список строк после первой строки-команды.
# Выход:
#   metadata_lines — строки вида "Ключ: значение" до разделителя ---.
#   body_lines — всё, что является медицинским входным текстом.
def split_metadata_and_body(lines: list[str]) -> tuple[list[str], list[str]]:
    metadata_lines: list[str] = []
    body_lines: list[str] = []
    in_body = False

    for line in lines:
        if line.strip() == "---" and not in_body:
            in_body = True
            continue
        if not in_body and re.match(r"^[А-Яа-яA-Za-z _-]+:\s*.+", line):
            metadata_lines.append(line)
            continue
        in_body = True
        body_lines.append(line)

    return metadata_lines, body_lines


# Назначение: применить одну metadata-строку к WorkflowMessage.
# Вход:
#   message — объект, который заполняем.
#   line — строка "Область: ОГК" / "Контекст: ..." / "Сравнение: да".
# Выход: тот же объект message изменяется на месте.
def apply_metadata_line(message: WorkflowMessage, line: str) -> None:
    key, value = line.split(":", 1)
    key = key.strip().lower().replace("ё", "е")
    value = value.strip()

    if key in {"область", "area", "зона"}:
        message.area = parse_area(value)
    elif key in {"контекст", "анамнез", "clinical context", "clinical_context"}:
        message.clinical_context = value
    elif key in {"сравнение", "динамика", "comparison"}:
        message.comparison = parse_bool(value)
    elif key in {"режим", "mode"}:
        if value in {"fast", "analytical"}:
            message.mode = value
    elif key in {"тип ввода", "input type", "input_type"}:
        if value in {"text", "markdown", "voice_transcript"}:
            message.input_type = value
    elif key in {"формат вывода", "output mode", "output_mode"}:
        if value in {"full_systematic", "findings_only"}:
            message.output_mode = value


# Назначение: найти внутри body отдельный блок "Черновик ассистента:".
# Вход: body-текст из Telegram/Hermes сообщения.
# Выход:
#   input_text — исходное описание/находки до маркера.
#   assistant_draft — черновик после маркера или пустая строка.
def split_assistant_draft(body_text: str) -> tuple[str, str]:
    pattern = re.compile(
        r"^\s*(?:черновик ассистента|вариант ассистента|assistant draft)\s*:\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(body_text)
    if not match:
        return body_text.strip(), ""
    return body_text[: match.start()].strip(), body_text[match.end() :].strip()


SESSION_BLOCK_MARKERS = {
    "assistant_draft": r"(?:черновик ассистента|вариант ассистента|assistant draft)",
    "roman_final": r"(?:финальный вариант|финал романа|roman final|final)",
    "feedback": r"(?:почему|feedback|объяснение правок|комментарий к правкам)",
}


# Назначение: разобрать body диалогового session-capture файла на блоки.
# Вход: markdown после metadata-разделителя ---; блоки задаются строками
#   "Черновик ассистента:", "Финальный вариант:", "Почему:".
# Выход: dict с ключами input_text / assistant_draft / roman_final / feedback.
def split_session_capture_blocks(body_text: str) -> dict[str, str]:
    marker_pattern = re.compile(
        r"^\s*(?P<marker>"
        + "|".join(SESSION_BLOCK_MARKERS.values())
        + r")\s*:\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    matches = list(marker_pattern.finditer(body_text))
    if not matches:
        return {
            "input_text": body_text.strip(),
            "assistant_draft": "",
            "roman_final": "",
            "feedback": "",
        }

    blocks: dict[str, str] = {"input_text": body_text[: matches[0].start()].strip()}
    for index, match in enumerate(matches):
        marker_text = match.group("marker").lower().replace("ё", "е")
        key = ""
        for candidate_key, candidate_pattern in SESSION_BLOCK_MARKERS.items():
            if re.fullmatch(candidate_pattern, marker_text, flags=re.IGNORECASE):
                key = candidate_key
                break
        if not key:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body_text)
        blocks[key] = body_text[start:end].strip()

    return {
        "input_text": blocks.get("input_text", "").strip(),
        "assistant_draft": blocks.get("assistant_draft", "").strip(),
        "roman_final": blocks.get("roman_final", "").strip(),
        "feedback": blocks.get("feedback", "").strip(),
    }


# Назначение: разобрать один файл, собранный из обычной Telegram/Hermes-сессии,
# в WorkflowMessage + финальный вариант + feedback.
# Вход: markdown с metadata и блоками: входной текст, Черновик ассистента,
# Финальный вариант, Почему.
# Выход: tuple(message, roman_final, feedback_text).
def parse_session_capture_message(text: str) -> tuple[WorkflowMessage, str, str]:
    raw_lines = text.replace("\r\n", "\n").splitlines()
    lines = [line.rstrip() for line in raw_lines]
    non_empty_indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not non_empty_indexes:
        raise ValueError("Session capture сообщение пустое")

    first_index = non_empty_indexes[0]
    first_line = lines[first_index]
    message = WorkflowMessage()
    detected_task = task_from_trigger(first_line)
    remaining = lines[first_index + 1 :] if detected_task else lines[first_index:]
    if detected_task:
        message.task = detected_task

    metadata_lines, body_lines = split_metadata_and_body(remaining)
    for metadata_line in metadata_lines:
        apply_metadata_line(message, metadata_line)

    blocks = split_session_capture_blocks("\n".join(body_lines).strip())
    if not blocks["input_text"]:
        raise ValueError("Не найден входной текст описания/находок")
    if not blocks["roman_final"]:
        raise ValueError("Не найден блок 'Финальный вариант:' для session capture")

    message.input_text = blocks["input_text"]
    message.assistant_draft = blocks["assistant_draft"]
    return message, blocks["roman_final"], blocks["feedback"]


# Назначение: разобрать Telegram/Hermes-сообщение в payload для /api/draft.
# Вход: полный текст сообщения.
# Выход: WorkflowMessage с task, area, context, comparison, input_text и assistant_draft.
# Ошибки: ValueError, если нет медицинского входного текста.
def parse_workflow_message(text: str) -> WorkflowMessage:
    raw_lines = text.replace("\r\n", "\n").splitlines()
    lines = [line.rstrip() for line in raw_lines]
    non_empty_indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not non_empty_indexes:
        raise ValueError("Сообщение пустое")

    first_index = non_empty_indexes[0]
    first_line = lines[first_index]
    message = WorkflowMessage()

    detected_task = task_from_trigger(first_line)
    if detected_task:
        message.task = detected_task
        if detected_task == "finding_description":
            message.output_mode = "findings_only"
        remaining = lines[first_index + 1 :]
    else:
        # Если явного инициатора нет, считаем всё сообщение входным текстом
        # для заключения. Это удобно для быстрых локальных smoke-тестов.
        remaining = lines[first_index:]

    metadata_lines, body_lines = split_metadata_and_body(remaining)
    for metadata_line in metadata_lines:
        apply_metadata_line(message, metadata_line)

    body_text = "\n".join(body_lines).strip()
    input_text, assistant_draft = split_assistant_draft(body_text)
    if not input_text.strip():
        raise ValueError("Не найден входной текст описания/находок")

    message.input_text = input_text
    message.assistant_draft = assistant_draft
    return message


# Назначение: собрать JSON payload для /api/draft.
# Вход: WorkflowMessage и опциональный assistant_draft из отдельного файла.
# Выход: словарь, совместимый с DraftRequest backend-а.
def draft_payload(
    message: WorkflowMessage,
    assistant_draft_override: str = "",
    references_used: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "input_text": message.input_text,
        "task": message.task,
        "input_type": message.input_type,
        "area": message.area,
        "clinical_context": message.clinical_context,
        "comparison": message.comparison,
        "mode": message.mode,
        "assistant_draft": assistant_draft_override.strip() or message.assistant_draft,
        "references_used": references_used or [],
    }


# Назначение: красиво обрезать длинный текст для Telegram-ответа.
# Вход: произвольный текст и максимальная длина.
# Выход: строка не длиннее limit, с многоточием при обрезке.
def preview(text: str, limit: int = 900) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


# Назначение: напечатать либо raw JSON, либо Markdown для Telegram/Hermes.
# Вход:
#   data — ответ API.
#   json_output — если True, печатаем pretty JSON.
#   markdown — готовая Markdown-строка для человека.
# Выход: текст в stdout.
def print_result(data: Any, json_output: bool, markdown: str) -> None:
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(markdown)


# Назначение: форматировать ответ создания draft для Telegram.
# Вход: JSON от /api/draft.
# Выход: Markdown с case_id, статусом и текстом черновика.
def format_draft_response(data: dict[str, Any]) -> str:
    references_used = data.get("references_used") or []
    refs_text = f"\n- Референсы: {len(references_used)}" if references_used else ""
    return (
        "## RadiCT draft создан\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Файл: `{data.get('path', '')}`{refs_text}\n\n"
        "**Черновик:**\n"
        f"{preview(data.get('draft', ''))}\n\n"
        "Дальше: если вариант верный — `accept CASE_ID`; если правишь — `correct CASE_ID --final ...`."
    )


# Назначение: форматировать RAG-context для Telegram/Hermes.
# Вход: JSON от /api/rag/context.
# Выход: Markdown с readiness и полным prompt, который Hermes должен использовать для черновика.
def format_rag_context_response(data: dict[str, Any]) -> str:
    references = data.get("references") or []
    lines = [
        "## RadiCT RAG context",
        f"- Референсы: {len(references)}",
    ]
    for ref in references[:10]:
        lines.append(
            f"- `{ref.get('filepath', '')}` — score {ref.get('similarity', 0)} / {ref.get('area', '') or '—'}"
        )
    lines.extend(["", "**Prompt для Hermes:**", "```text", data.get("prompt", ""), "```"])
    return "\n".join(lines)


# Назначение: форматировать ответ accept/correct для Telegram.
# Вход: JSON от /api/accept или /api/correct.
# Выход: Markdown с новым статусом case и проверенным reference outcome.
def format_action_response(data: dict[str, Any], title: str) -> str:
    saved = "да" if data.get("saved_as_reference") else "нет"
    ref = data.get("reference") or {}
    ref_lines = []
    if ref:
        ref_lines.append(f"- Reference requested: `{ref.get('requested', False)}`")
        if ref.get("saved"):
            ref_lines.append(f"- Reference ID: `{ref.get('reference_id', '')}`")
            ref_lines.append(f"- Reference path: `{ref.get('path', '')}`")
            idx = "да" if ref.get("index_updated") else "нет"
            ref_lines.append(f"- Index updated: {idx}")
            if ref.get("index_error"):
                ref_lines.append(f"- ⚠️ Index error: `{ref.get('index_error', '')}`")
        elif ref.get("skip_reason"):
            ref_lines.append(f"- Skip reason: `{ref.get('skip_reason', '')}`")
    ref_text = "\n".join(ref_lines)
    return (
        f"## {title}\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Статус: `{data.get('status', '')}`\n"
        f"- Файл: `{data.get('path', '')}`\n"
        f"- Сохранено как reference: {saved}\n"
        f"{ref_text}"
    )


# Назначение: форматировать ответ автоматического захвата диалогового кейса.
# Вход: dict с draft/correct API-ответами.
# Выход: Markdown с case_id и проверенным reference outcome.
def format_capture_session_response(data: dict[str, Any]) -> str:
    correct_data = data.get("correct", {})
    saved = "да" if correct_data.get("saved_as_reference") else "нет"
    ref = correct_data.get("reference") or {}
    ref_lines = []
    if ref:
        if ref.get("saved"):
            ref_lines.append(f"- Reference ID: `{ref.get('reference_id', '')}`")
            idx = "да" if ref.get("index_updated") else "нет"
            ref_lines.append(f"- Index updated: {idx}")
            if ref.get("index_error"):
                ref_lines.append(f"- ⚠️ Index error: `{ref.get('index_error', '')}`")
        elif ref.get("skip_reason"):
            ref_lines.append(f"- Skip reason: `{ref.get('skip_reason', '')}`")
    ref_text = "\n".join(ref_lines)
    return (
        "## RadiCT session capture сохранен\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Статус: `{correct_data.get('status', '')}`\n"
        f"- Файл case: `{correct_data.get('path', '')}`\n"
        f"- Сохранено как reference: {saved}\n"
        f"{ref_text}\n"
        "- PHI guard: выполнен backend-ом при promotion"
    )


# Назначение: форматировать список cases для Telegram.
# Вход: JSON-список CaseSummary.
# Выход: компактный Markdown-список последних case.
def format_cases_response(data: list[dict[str, Any]]) -> str:
    if not data:
        return "Cases не найдены."
    lines = ["## RadiCT cases"]
    for item in data[:20]:
        area = ", ".join(item.get("area") or []) or "—"
        lines.append(
            f"- `{item.get('case_id', '')}` — `{item.get('status', '')}` / {item.get('task', '')} / {area}"
        )
    if len(data) > 20:
        lines.append(f"\nПоказаны первые 20 из {len(data)}.")
    return "\n".join(lines)


# Назначение: форматировать полный case для Telegram.
# Вход: JSON CaseDetail.
# Выход: Markdown с основными полями и preview текста.
def format_case_response(data: dict[str, Any]) -> str:
    feedback = data.get("feedback") or []
    tags = data.get("error_tags") or []
    return (
        "## RadiCT case\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Статус: `{data.get('status', '')}`\n"
        f"- Задача: `{data.get('task', '')}`\n"
        f"- Область: {', '.join(data.get('area') or []) or '—'}\n"
        f"- Feedback: {len(feedback)} пункт(ов)\n"
        f"- Tags: {', '.join(tags) if tags else '—'}\n\n"
        "**Черновик ассистента:**\n"
        f"{preview(data.get('assistant_draft', ''), 700)}\n\n"
        "**Финальный вариант:**\n"
        f"{preview(data.get('roman_final', ''), 700)}"
    )


# Назначение: форматировать список lesson candidates для Telegram.
# Вход: JSON-список LessonInfo.
# Выход: Markdown со списком кандидатов правил.
def format_lessons_response(data: list[dict[str, Any]]) -> str:
    if not data:
        return "Lesson candidates не найдены."
    lines = ["## Lesson candidates"]
    for item in data[:10]:
        lines.append(f"- `{item.get('path', '')}`\n  {preview(item.get('content', ''), 240)}")
    if len(data) > 10:
        lines.append(f"\nПоказаны первые 10 из {len(data)}.")
    return "\n".join(lines)


# Назначение: команда `health` — проверить доступность API.
# Вход: argparse args.
# Выход: JSON или Markdown в stdout.
def cmd_health(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", "/api/health")
    print_result(data, args.json, f"RadiCT API: `{data.get('status', 'unknown')}`")


# Назначение: команда `rag-status` — проверить готовность RAG/OHS.
# Вход: argparse args.
# Выход: JSON или компактный Markdown со статусом индекса.
def cmd_rag_status(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", "/api/rag/status")
    markdown = (
        "## RadiCT RAG status\n"
        f"- Backend: `{data.get('backend', '')}`\n"
        f"- Available: `{data.get('available', False)}`\n"
        f"- Notes: `{data.get('indexed', 0)}/{data.get('total', 0)}`\n"
        f"- Chunks: `{data.get('chunks', 0)}`\n"
        f"- Command: `{data.get('command', '')}`\n"
        f"- Error: `{data.get('error', '') or '—'}`"
    )
    print_result(data, args.json, markdown)


# Назначение: команда `rag-context` — собрать local few-shot prompt для Hermes.
# Вход: сообщение/описание в том же формате, что `message`.
# Выход: prompt + references_used; backend не вызывает LLM.
def cmd_rag_context(args: argparse.Namespace) -> None:
    workflow_message = parse_workflow_message(read_text(args.input))
    output_mode = args.output_mode or workflow_message.output_mode
    if workflow_message.task == "finding_description":
        output_mode = "findings_only"
    payload = {
        "input_text": workflow_message.input_text,
        "task": workflow_message.task,
        "area": workflow_message.area,
        "clinical_context": workflow_message.clinical_context,
        "mode": workflow_message.mode,
        "top_k": args.top_k,
        "output_mode": output_mode,
    }
    data = radi_ct_api.request_json("POST", "/api/rag/context", payload=payload)
    print_result(data, args.json, format_rag_context_response(data))


# Назначение: команда `prepare` — единая operational entry point.
# Вход: сообщение/описание в формате workflow message или свободный текст.
# Выход: structured JSON с normalized metadata, prompt, references, rag_status.
# Hermes использует prompt для генерации черновика, затем вызывает save-draft.
def cmd_prepare(args: argparse.Namespace) -> None:
    workflow_message = parse_workflow_message(read_text(args.input))
    output_mode = args.output_mode or workflow_message.output_mode
    if workflow_message.task == "finding_description":
        output_mode = "findings_only"
    payload = {
        "input_text": workflow_message.input_text,
        "task": workflow_message.task,
        "area": workflow_message.area,
        "clinical_context": workflow_message.clinical_context,
        "comparison": workflow_message.comparison,
        "mode": workflow_message.mode,
        "top_k": args.top_k,
        "output_mode": output_mode,
    }
    data = radi_ct_api.request_json("POST", "/api/prepare", payload=payload)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    # Markdown output for Hermes
    refs = data.get("references") or []
    rag = data.get("rag_status", "unknown")
    lines = [
        "## RadiCT prepare",
        f"- Task: `{data.get('normalized', {}).get('task', '')}`",
        f"- Area: {', '.join(data.get('normalized', {}).get('area', []) or []) or '—'}",
        f"- RAG status: `{rag}`",
        f"- References found: {len(refs)}",
    ]
    for ref in refs[:5]:
        lines.append(f"  - `{ref.get('filepath', '')}` — score {ref.get('similarity', 0)}")
    lines.extend(["", "**Prompt for Hermes:**", "```text", data.get("prompt", ""), "```", ""])
    lines.append("Next: generate draft from prompt, then `save-draft --prepared PREPARED_JSON --draft DRAFT_FILE`")
    print("\n".join(lines))


# Назначение: команда `save-draft` — создать draft case из prepared JSON + assistant_draft.
# Вход: --prepared (JSON from prepare), --draft (file with Hermes-generated draft text).
# Выход: case_id для последующего accept/correct.
def cmd_save_draft(args: argparse.Namespace) -> None:
    prepared_json = read_text(args.prepared) if args.prepared != "-" else sys.stdin.read()
    try:
        prepared = json.loads(prepared_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid prepared JSON: {e}")

    draft_text = read_text(args.draft) if args.draft else ""
    if not draft_text.strip():
        raise SystemExit("save-draft requires --draft DRAFT_FILE with Hermes-generated draft text")

    payload = {
        "prepared": prepared,
        "assistant_draft": draft_text.strip(),
        "references_used": prepared.get("references_used", []),
    }
    data = radi_ct_api.request_json("POST", "/api/save-draft", payload=payload)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    refs = data.get("references_used") or []
    refs_text = f"\n- References: {len(refs)}" if refs else ""
    print(
        f"## RadiCT draft сохранён\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Файл: `{data.get('path', '')}`{refs_text}\n"
        f"\n**Черновик:**\n{preview(data.get('draft', ''))}\n\n"
        f"Дальше: `accept CASE_ID` или `correct CASE_ID --final ...`"
    )


# Назначение: команда `message` — создать draft из Telegram/Hermes-сообщения.
# Вход:
#   args.input — файл сообщения или stdin.
#   args.assistant_draft — необязательный отдельный файл с черновиком Hermes.
# Выход: Markdown-ответ с case_id и draft.
def cmd_message(args: argparse.Namespace) -> None:
    workflow_message = parse_workflow_message(read_text(args.input))
    assistant_draft_override = read_text(args.assistant_draft) if args.assistant_draft else ""
    if not (assistant_draft_override.strip() or workflow_message.assistant_draft.strip()):
        raise SystemExit(
            "Hermes-only mode requires an assistant draft. Add a 'Черновик ассистента:' "
            "block to the message or pass --assistant-draft PATH."
        )
    data = radi_ct_api.request_json(
        "POST",
        "/api/draft",
        payload=draft_payload(workflow_message, assistant_draft_override=assistant_draft_override),
    )
    print_result(data, args.json, format_draft_response(data))


# Назначение: команда `accept` — принять draft без правок.
# Вход: case_id и флаг --no-save-as-reference.
# Выход: Markdown с новым статусом.
def cmd_accept(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json(
        "POST",
        f"/api/accept/{args.case_id}",
        payload={"save_as_reference": not args.no_save_as_reference},
    )
    print_result(data, args.json, format_action_response(data, "RadiCT case принят"))


# Назначение: команда `correct` — сохранить финальный вариант и feedback.
# Вход: case_id, --final, optional --feedback/--tag/--no-save-as-reference.
# Выход: Markdown с новым статусом.
def cmd_correct(args: argparse.Namespace) -> None:
    feedback = radi_ct_api.parse_feedback_items(read_text(args.feedback)) if args.feedback else []
    payload = {
        "roman_final": read_text(args.final),
        "feedback": feedback,
        "error_tags": args.tag or [],
        "save_as_reference": not args.no_save_as_reference,
        "create_lesson_candidate": args.create_lesson_candidate,
    }
    data = radi_ct_api.request_json("POST", f"/api/correct/{args.case_id}", payload=payload)
    print_result(data, args.json, format_action_response(data, "RadiCT case исправлен"))


# Назначение: команда `capture-session` — одним вызовом сохранить обычную
# Telegram/Hermes-сессию как corrected case и, по умолчанию, reference.
# Вход: markdown-файл с input, Черновик ассистента, Финальный вариант, Почему.
# Выход: Markdown/JSON с созданным case_id; backend выполняет PHI guard.
def cmd_capture_session(args: argparse.Namespace) -> None:
    workflow_message, roman_final, feedback_text = parse_session_capture_message(read_text(args.input))
    assistant_draft_override = read_text(args.assistant_draft) if args.assistant_draft else ""
    assistant_draft = assistant_draft_override.strip() or workflow_message.assistant_draft.strip()
    if not assistant_draft:
        if args.use_final_as_draft:
            assistant_draft = roman_final.strip()
        else:
            raise SystemExit(
                "Session capture requires an assistant draft. Add a 'Черновик ассистента:' "
                "block, pass --assistant-draft PATH, or explicitly use --use-final-as-draft."
            )

    draft_data = radi_ct_api.request_json(
        "POST",
        "/api/draft",
        payload=draft_payload(workflow_message, assistant_draft_override=assistant_draft),
    )
    case_id = draft_data["case_id"]
    feedback = radi_ct_api.parse_feedback_items(feedback_text)
    correct_payload = {
        "roman_final": roman_final,
        "feedback": feedback,
        "error_tags": args.tag or [],
        "save_as_reference": not args.no_save_as_reference,
        "create_lesson_candidate": args.create_lesson_candidate,
    }
    correct_data = radi_ct_api.request_json("POST", f"/api/correct/{case_id}", payload=correct_payload)
    data = {"case_id": case_id, "draft": draft_data, "correct": correct_data}
    print_result(data, args.json, format_capture_session_response(data))


# Назначение: команда `cases` — показать список cases.
# Вход: optional --status draft/accepted/corrected.
# Выход: Markdown-список.
def cmd_cases(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", "/api/cases", query={"status": args.status})
    print_result(data, args.json, format_cases_response(data))


# Назначение: команда `case` — показать один case.
# Вход: case_id.
# Выход: Markdown с деталями.
def cmd_case(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", f"/api/cases/{args.case_id}")
    print_result(data, args.json, format_case_response(data))


# Назначение: команда `promote` — явно перенести case в reference base.
# Вход: case_id accepted/corrected case.
# Выход: Markdown с проверенным reference outcome.
def cmd_promote(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("POST", f"/api/references/promote/{args.case_id}", payload={})
    ref = data.get("reference") or {}
    ref_lines = []
    if ref:
        if ref.get("saved"):
            ref_lines.append(f"- Reference ID: `{ref.get('reference_id', '')}`")
            ref_lines.append(f"- Reference path: `{ref.get('path', '')}`")
            idx = "да" if ref.get("index_updated") else "нет"
            ref_lines.append(f"- Index updated: {idx}")
            if ref.get("index_error"):
                ref_lines.append(f"- ⚠️ Index error: `{ref.get('index_error', '')}`")
        elif ref.get("skip_reason"):
            ref_lines.append(f"- Skip reason: `{ref.get('skip_reason', '')}`")
    ref_text = "\n".join(ref_lines)
    markdown = (
        "## Case сохранён в reference base\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Reference: `{data.get('reference_path', '')}`\n"
        f"{ref_text}"
    )
    print_result(data, args.json, markdown)


# Назначение: команда `lessons` — показать кандидаты правил стиля.
# Вход: ничего.
# Выход: Markdown-список lesson candidates.
def cmd_lessons(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", "/api/lessons")
    print_result(data, args.json, format_lessons_response(data))


# Назначение: команда `audit-status` — показать состояние RadiCT cases и инварианты.
# Вход: опционально --strict для проверки инвариантов с ненулевым exit code.
# Выход: Markdown-отчет со статистикой cases, references, session state.
def cmd_audit_status(args: argparse.Namespace) -> None:
    cases = radi_ct_api.request_json("GET", "/api/cases")
    rag_data = radi_ct_api.request_json("GET", "/api/rag/status")

    draft_count = sum(1 for c in cases if c.get("status") == "draft")
    accepted_count = sum(1 for c in cases if c.get("status") == "accepted")
    corrected_count = sum(1 for c in cases if c.get("status") == "corrected")

    # Try to get session state listing
    pending = {}
    try:
        state_data = radi_ct_api.request_json("GET", "/api/session/states")
        pending = {k: v for k, v in state_data.items() if v.get("state") == "capture_pending"}
    except Exception:
        pass

    issues = []
    if draft_count > 0:
        issues.append(f"- {draft_count} незавершенных draft cases")
    if pending:
        issues.append(f"- {len(pending)} capture_pending sessions")
    if rag_data.get("available") and rag_data.get("indexed", 0) < rag_data.get("total", 0):
        issues.append(f"- Indexed {rag_data.get('indexed')}/{rag_data.get('total')} notes")

    markdown_lines = [
        "## RadiCT audit status",
        f"- Draft cases: {draft_count}",
        f"- Accepted cases: {accepted_count}",
        f"- Corrected cases: {corrected_count}",
        f"- RAG available: `{rag_data.get('available', False)}`",
        f"- RAG indexed: `{rag_data.get('indexed', 0)}/{rag_data.get('total', 0)}`",
        f"- Capture pending: {len(pending)}",
    ]
    if issues:
        markdown_lines.append("")
        markdown_lines.append("**Issues:**")
        markdown_lines.extend(issues)

    if args.strict and issues:
        markdown_lines.append("")
        markdown_lines.append("**--strict: FAIL**")
        print("\n".join(markdown_lines))
        raise SystemExit(1)
    elif args.strict:
        markdown_lines.append("")
        markdown_lines.append("**--strict: PASS**")

    print("\n".join(markdown_lines))


# Назначение: собрать CLI parser для workflow-wrapper.
# Вход: ничего.
# Выход: argparse.ArgumentParser с командами health/message/accept/correct/cases/case/promote/lessons.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadiCT Telegram/Hermes workflow wrapper")
    parser.add_argument(
        "--api-url",
        default=radi_ct_api.API_URL,
        help=f"RadiCT API URL (default: env RADI_CT_API_URL or {radi_ct_api.DEFAULT_API_URL})",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of Telegram Markdown")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Check API health")
    health.set_defaults(func=cmd_health)

    rag_status = subparsers.add_parser("rag-status", help="Check RAG/OHS readiness")
    rag_status.set_defaults(func=cmd_rag_status)

    rag_context = subparsers.add_parser("rag-context", help="Build local RAG/few-shot context for Hermes")
    rag_context.add_argument("input", help="Message markdown/text file, or '-' for stdin")
    rag_context.add_argument("--top-k", type=int, default=5, help="Number of references to retrieve")
    rag_context.add_argument(
        "--output-mode",
        choices=["full_systematic", "findings_only"],
        help="Override output mode from message metadata",
    )
    rag_context.set_defaults(func=cmd_rag_context)

    prepare = subparsers.add_parser("prepare", help="Unified operational entry point: parse + RAG + metadata")
    prepare.add_argument("input", help="Message markdown/text file, or '-' for stdin")
    prepare.add_argument("--top-k", type=int, default=5, help="Number of references to retrieve")
    prepare.add_argument(
        "--output-mode",
        choices=["full_systematic", "findings_only"],
        help="Override output mode from message metadata",
    )
    prepare.set_defaults(func=cmd_prepare)

    save_draft = subparsers.add_parser("save-draft", help="Create draft case from prepared JSON + assistant draft")
    save_draft.add_argument("--prepared", required=True, help="JSON from prepare, or '-' for stdin")
    save_draft.add_argument("--draft", help="File with Hermes-generated draft text")
    save_draft.set_defaults(func=cmd_save_draft)

    message = subparsers.add_parser("message", help="Create draft from Telegram/Hermes message")
    message.add_argument("input", help="Message markdown/text file, or '-' for stdin")
    message.add_argument("--assistant-draft", help="Optional assistant draft file; avoids LLM when provided")
    message.set_defaults(func=cmd_message)

    accept = subparsers.add_parser("accept", help="Accept draft case")
    accept.add_argument("case_id")
    accept.add_argument(
        "--no-save-as-reference",
        action="store_true",
        help="Do not auto-promote accepted case to reference base",
    )
    accept.set_defaults(func=cmd_accept)

    correct = subparsers.add_parser("correct", help="Correct case")
    correct.add_argument("case_id")
    correct.add_argument("--final", required=True, help="Final text file, or '-' for stdin")
    correct.add_argument("--feedback", help="Feedback text file")
    correct.add_argument("--tag", action="append", help="Error tag, can be repeated")
    correct.add_argument(
        "--no-save-as-reference",
        action="store_true",
        help="Do not auto-promote corrected case to reference base",
    )
    correct.add_argument("--create-lesson-candidate", action="store_true")
    correct.set_defaults(func=cmd_correct)

    capture_session = subparsers.add_parser(
        "capture-session",
        help="Capture a normal Telegram/Hermes radiology session as corrected case + reference",
    )
    capture_session.add_argument("input", help="Session capture markdown/text file, or '-' for stdin")
    capture_session.add_argument("--assistant-draft", help="Optional assistant draft file")
    capture_session.add_argument(
        "--use-final-as-draft",
        action="store_true",
        help="Use Roman final as assistant_draft when no assistant draft is available",
    )
    capture_session.add_argument("--tag", action="append", help="Error/style tag, can be repeated")
    capture_session.add_argument(
        "--no-save-as-reference",
        action="store_true",
        help="Create corrected case but do not auto-promote to reference base",
    )
    capture_session.add_argument("--create-lesson-candidate", action="store_true")
    capture_session.set_defaults(func=cmd_capture_session)

    cases = subparsers.add_parser("cases", help="List cases")
    cases.add_argument("--status", choices=["draft", "accepted", "corrected"])
    cases.set_defaults(func=cmd_cases)

    case = subparsers.add_parser("case", help="Show full case")
    case.add_argument("case_id")
    case.set_defaults(func=cmd_case)

    promote = subparsers.add_parser("promote", help="Promote case to reference base")
    promote.add_argument("case_id")
    promote.set_defaults(func=cmd_promote)

    lessons = subparsers.add_parser("lessons", help="List lesson candidates")
    lessons.set_defaults(func=cmd_lessons)

    audit_status = subparsers.add_parser("audit-status", help="Show case/reference/session audit status")
    audit_status.add_argument("--strict", action="store_true", help="Non-zero exit on invariant violations")
    audit_status.set_defaults(func=cmd_audit_status)

    return parser


# Назначение: точка входа CLI.
# Вход: argv или None.
# Выход: печатает Markdown/JSON; при ошибке парсинга печатает понятный текст в stderr и exit 2.
def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    radi_ct_api.API_URL = args.api_url.rstrip("/")
    try:
        args.func(args)
    except ValueError as error:
        print(f"Workflow message error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
