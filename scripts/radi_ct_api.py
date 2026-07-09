#!/usr/bin/env python3
"""
HTTP CLI-клиент для RadiCT Assistant API.

Зачем нужен этот файл:
    Это тонкий интеграционный слой между FastAPI backend и внешними рабочими
    поверхностями: Hermes/Telegram, Obsidian Templater, shell-скрипты.

    В отличие от scripts/radi_ct.py, этот CLI НЕ пишет напрямую в локальное
    хранилище. Он отправляет HTTP-запросы в запущенный FastAPI сервис.

Базовая настройка:
    1. Запустить API:
        uvicorn src.main:app --host 127.0.0.1 --port 8000

    2. Проверить health:
        python3 scripts/radi_ct_api.py health

    3. Создать draft без LLM, если черновик уже готов:
        python3 scripts/radi_ct_api.py draft input.md --assistant-draft draft.md --area ОГК

    4. Исправить case:
        python3 scripts/radi_ct_api.py correct CASE_ID --final final.md --feedback feedback.md --tag incomplete_stable_findings_list

    5. Явно сохранить corrected/accepted case в reference base:
        python3 scripts/radi_ct_api.py promote CASE_ID

Переменные окружения:
    RADI_CT_API_URL — URL API сервиса.
        По умолчанию: http://127.0.0.1:8000

Безопасность:
    CLI сам не обезличивает текст. Он только передает его в локальный API.
    Для сохранения в reference base backend дополнительно выполняет базовый
    PHI guard. Реальные пациентские данные нельзя отправлять во внешние LLM/API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "http://127.0.0.1:8000"
API_URL = os.getenv("RADI_CT_API_URL", DEFAULT_API_URL).rstrip("/")


# Назначение: прочитать текст из файла или stdin.
# Вход:
#   path — путь к UTF-8 файлу, None или "-".
#   Если path равен None или "-", читаем stdin. Это удобно для pipe-сценариев:
#       pbpaste | python3 scripts/radi_ct_api.py draft - --area ОГК
# Выход: строка с исходным текстом.
def read_text_arg(path: str | None) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


# Назначение: превратить feedback-файл или строку из stdin в список пунктов.
# Вход: текст вида:
#       - пункт 1
#       - пункт 2
#   или обычные непустые строки.
# Выход: ["пункт 1", "пункт 2"].
def parse_feedback_items(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        items.append(line)
    return items


# Назначение: вывести JSON одинаково для человека, shell и тестов.
# Вход: любой JSON-сериализуемый объект.
# Выход: pretty JSON в stdout, UTF-8 без ASCII-экранирования.
def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# Назначение: собрать полный URL endpoint-а с query-параметрами.
# Вход:
#   path — путь вида "/api/cases".
#   query — словарь query-параметров; None-значения пропускаются.
# Выход: строка полного URL.
def build_url(path: str, query: dict[str, Any] | None = None) -> str:
    url = f"{API_URL}{path}"
    if not query:
        return url
    clean_query = {key: value for key, value in query.items() if value is not None}
    if not clean_query:
        return url
    return f"{url}?{urllib.parse.urlencode(clean_query, doseq=True)}"


# Назначение: выполнить HTTP-запрос к RadiCT API и разобрать JSON.
# Вход:
#   method — "GET" или "POST".
#   path — endpoint, например "/api/draft".
#   payload — JSON body для POST.
#   query — query-параметры для GET/POST.
# Выход: JSON-ответ как dict/list.
# Ошибки:
#   При HTTP 4xx/5xx печатает тело ошибки в stderr и завершает процесс с кодом 1.
def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    url = build_url(path, query=query)
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_text = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} {e.reason}: {error_text}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"Cannot reach RadiCT API at {API_URL}: {e}", file=sys.stderr)
        raise SystemExit(1)

    if not response_text.strip():
        return None
    return json.loads(response_text)


# Назначение: проверить, что API доступен.
# Вход: argparse namespace.
# Выход: JSON ответа /api/health.
def cmd_health(args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/api/health"))


# Назначение: создать draft case через HTTP API.
# Вход:
#   input — файл со входным описанием/черновыми находками или "-" для stdin.
#   --assistant-draft — файл с уже готовым черновиком; если не передан, backend
#       попытается использовать generate path с retrieval/LLM.
# Выход: JSON с case_id, draft, references_used, path.
def cmd_draft(args: argparse.Namespace) -> None:
    payload = {
        "input_text": read_text_arg(args.input),
        "task": args.task,
        "input_type": args.input_type,
        "area": args.area or [],
        "clinical_context": args.clinical_context or "",
        "comparison": args.comparison,
        "mode": args.mode,
        "assistant_draft": read_text_arg(args.assistant_draft).strip() if args.assistant_draft else "",
    }
    print_json(request_json("POST", "/api/draft", payload=payload))


# Назначение: принять draft без правок.
# Вход: case_id и опциональный --save-as-reference.
# Выход: JSON с новым статусом и путем.
def cmd_accept(args: argparse.Namespace) -> None:
    payload = {"save_as_reference": args.save_as_reference}
    print_json(request_json("POST", f"/api/accept/{args.case_id}", payload=payload))


# Назначение: сохранить финальный вариант Романа и feedback.
# Вход:
#   case_id — идентификатор case.
#   --final — файл с финальным текстом или "-" для stdin.
#   --feedback — файл с объяснениями правок.
#   --tag — теги ошибок, можно повторять.
# Выход: JSON с новым статусом и путем.
def cmd_correct(args: argparse.Namespace) -> None:
    feedback = parse_feedback_items(read_text_arg(args.feedback)) if args.feedback else []
    payload = {
        "roman_final": read_text_arg(args.final),
        "feedback": feedback,
        "error_tags": args.tag or [],
        "save_as_reference": args.save_as_reference,
        "create_lesson_candidate": args.create_lesson_candidate,
    }
    print_json(request_json("POST", f"/api/correct/{args.case_id}", payload=payload))


# Назначение: вывести список cases через API.
# Вход: опциональный --status draft/accepted/corrected.
# Выход: JSON-список CaseSummary.
def cmd_cases(args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/api/cases", query={"status": args.status}))


# Назначение: вывести полный case через API.
# Вход: case_id.
# Выход: JSON CaseDetail.
def cmd_case(args: argparse.Namespace) -> None:
    print_json(request_json("GET", f"/api/cases/{args.case_id}"))


# Назначение: явно перенести accepted/corrected case в reference base через API.
# Вход: case_id.
# Выход: JSON с reference_path. Backend выполняет PHI guard.
def cmd_promote(args: argparse.Namespace) -> None:
    print_json(request_json("POST", f"/api/references/promote/{args.case_id}", payload={}))


# Назначение: вывести lifecycle metadata reference base через API.
def cmd_references(args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/api/references/lifecycle", query={"status": args.status, "include_inactive": not args.active_only}))


# Назначение: обновить lifecycle metadata reference через API.
def cmd_reference_update(args: argparse.Namespace) -> None:
    payload = {
        "reference_status": args.reference_status,
        "quality": args.quality,
        "style_version": args.style_version,
    }
    print_json(request_json("POST", f"/api/references/lifecycle/{args.reference_id}", payload=payload))


# Назначение: вывести lesson candidates через API.
# Вход: ничего.
# Выход: JSON-список LessonInfo.
def cmd_lessons(args: argparse.Namespace) -> None:
    print_json(request_json("GET", "/api/lessons"))


# Назначение: собрать argparse CLI с командами, соответствующими API endpoints.
# Вход: ничего.
# Выход: настроенный parser.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadiCT Assistant HTTP API CLI")
    parser.add_argument(
        "--api-url",
        default=API_URL,
        help=f"RadiCT API URL (default: env RADI_CT_API_URL or {DEFAULT_API_URL})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Check API health")
    health.set_defaults(func=cmd_health)

    draft = subparsers.add_parser("draft", help="Create draft case through API")
    draft.add_argument("input", help="Input markdown/text file, or '-' for stdin")
    draft.add_argument("--assistant-draft", help="Assistant draft file; avoids LLM if provided")
    draft.add_argument("--task", default="conclusion", choices=["conclusion", "description", "description_and_conclusion", "edit_description", "edit_conclusion"])
    draft.add_argument("--input-type", default="markdown", choices=["text", "markdown", "voice_transcript"])
    draft.add_argument("--area", action="append", help="Study area, can be repeated")
    draft.add_argument("--clinical-context", default="", help="Anonymized clinical context")
    draft.add_argument("--comparison", action="store_true", help="Dynamic comparison exists")
    draft.add_argument("--mode", default="fast", choices=["fast", "analytical"], help="Generation mode if assistant draft is omitted")
    draft.set_defaults(func=cmd_draft)

    accept = subparsers.add_parser("accept", help="Accept draft case through API")
    accept.add_argument("case_id")
    accept.add_argument("--save-as-reference", action="store_true")
    accept.set_defaults(func=cmd_accept)

    correct = subparsers.add_parser("correct", help="Correct case through API")
    correct.add_argument("case_id")
    correct.add_argument("--final", required=True, help="Final text file, or '-' for stdin")
    correct.add_argument("--feedback", help="Feedback text file")
    correct.add_argument("--tag", action="append", help="Error tag, can be repeated")
    correct.add_argument("--save-as-reference", action="store_true")
    correct.add_argument("--create-lesson-candidate", action="store_true")
    correct.set_defaults(func=cmd_correct)

    cases = subparsers.add_parser("cases", help="List cases through API")
    cases.add_argument("--status", choices=["draft", "accepted", "corrected"])
    cases.set_defaults(func=cmd_cases)

    case = subparsers.add_parser("case", help="Show full case through API")
    case.add_argument("case_id")
    case.set_defaults(func=cmd_case)

    promote = subparsers.add_parser("promote", help="Promote case to reference through API")
    promote.add_argument("case_id")
    promote.set_defaults(func=cmd_promote)

    references = subparsers.add_parser("references", help="List reference lifecycle metadata through API")
    references.add_argument("--status", choices=["active", "gold", "deprecated", "needs_review", "rejected"])
    references.add_argument("--active-only", action="store_true")
    references.set_defaults(func=cmd_references)

    reference_update = subparsers.add_parser("reference-update", help="Update reference lifecycle metadata through API")
    reference_update.add_argument("reference_id")
    reference_update.add_argument("--reference-status", choices=["active", "gold", "deprecated", "needs_review", "rejected"])
    reference_update.add_argument("--quality", choices=["gold", "high", "standard", "low"])
    reference_update.add_argument("--style-version")
    reference_update.set_defaults(func=cmd_reference_update)

    lessons = subparsers.add_parser("lessons", help="List lesson candidates through API")
    lessons.set_defaults(func=cmd_lessons)

    return parser


# Назначение: точка входа CLI.
# Вход: argv или None.
# Выход: печатает JSON в stdout; при HTTP-ошибке завершает процесс с кодом 1.
def main(argv: list[str] | None = None) -> None:
    global API_URL
    parser = build_parser()
    args = parser.parse_args(argv)
    API_URL = args.api_url.rstrip("/")
    args.func(args)


if __name__ == "__main__":
    main()
