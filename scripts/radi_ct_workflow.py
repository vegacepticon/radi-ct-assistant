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

    3. Создать draft из Telegram/Hermes-сообщения:
        python3 scripts/radi_ct_workflow.py message message.md

    4. Сохранить исправление Романа:
        python3 scripts/radi_ct_workflow.py correct CASE_ID --final final.md --feedback feedback.md --tag incomplete_stable_findings_list

    В Telegram/Hermes workflow accept/correct по умолчанию также пытаются
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

Если Hermes уже подготовил черновик и нужно только сохранить case без вызова LLM:

    РКТ заключение
    Область: ОГК
    ---
    Описание: синтетическое описание.

    Черновик ассистента:
    Синтетическое заключение.

Команды пользователя, которые распознаёт wrapper:
    - РКТ заключение
    - РКТ описание
    - РКТ описание + заключение
    - Исправляю: / Почему: — для correction-сценария удобнее явная команда
      `correct`, потому что нужен case_id.

Безопасность:
    Wrapper сам не обезличивает текст. Он только передает текст в локальный API.
    Не отправляйте реальные идентификаторы пациента во внешние LLM/API.
    В Telegram/Hermes workflow сохранение в reference base включено по
    умолчанию для accept/correct, потому что это часть радиологического
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
def draft_payload(message: WorkflowMessage, assistant_draft_override: str = "") -> dict[str, Any]:
    return {
        "input_text": message.input_text,
        "task": message.task,
        "input_type": message.input_type,
        "area": message.area,
        "clinical_context": message.clinical_context,
        "comparison": message.comparison,
        "mode": message.mode,
        "assistant_draft": assistant_draft_override.strip() or message.assistant_draft,
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


# Назначение: форматировать ответ accept/correct для Telegram.
# Вход: JSON от /api/accept или /api/correct.
# Выход: Markdown с новым статусом case.
def format_action_response(data: dict[str, Any], title: str) -> str:
    saved = "да" if data.get("saved_as_reference") else "нет"
    return (
        f"## {title}\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Статус: `{data.get('status', '')}`\n"
        f"- Файл: `{data.get('path', '')}`\n"
        f"- Сохранено как reference: {saved}"
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


# Назначение: команда `message` — создать draft из Telegram/Hermes-сообщения.
# Вход:
#   args.input — файл сообщения или stdin.
#   args.assistant_draft — необязательный отдельный файл с черновиком Hermes.
# Выход: Markdown-ответ с case_id и draft.
def cmd_message(args: argparse.Namespace) -> None:
    workflow_message = parse_workflow_message(read_text(args.input))
    assistant_draft_override = read_text(args.assistant_draft) if args.assistant_draft else ""
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
# Выход: Markdown с path созданного reference-файла.
def cmd_promote(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("POST", f"/api/references/promote/{args.case_id}", payload={})
    markdown = (
        "## Case сохранён в reference base\n"
        f"- Case ID: `{data.get('case_id', '')}`\n"
        f"- Reference: `{data.get('reference_path', '')}`"
    )
    print_result(data, args.json, markdown)


# Назначение: команда `lessons` — показать кандидаты правил стиля.
# Вход: ничего.
# Выход: Markdown-список lesson candidates.
def cmd_lessons(args: argparse.Namespace) -> None:
    data = radi_ct_api.request_json("GET", "/api/lessons")
    print_result(data, args.json, format_lessons_response(data))


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
