#!/usr/bin/env python3
"""
CLI для локального learning loop RadiCT Assistant.

Примеры:
    python scripts/radi_ct.py draft input.md --task conclusion --area ОГК --draft draft.md
    python scripts/radi_ct.py accept 2026-07-06-001 --save-as-reference
    python scripts/radi_ct.py correct 2026-07-06-001 --final final.md --feedback feedback.md --tag missed_resolved_finding
    python scripts/radi_ct.py cases
    python scripts/radi_ct.py lessons
    python scripts/radi_ct.py promote 2026-07-06-001

CLI не вызывает LLM. Он только сохраняет локальные case/feedback/reference файлы.
Для реальных клинических данных использовать только после обезличивания.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Скрипт запускается из scripts/, поэтому добавляем корень репозитория в sys.path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.feedback_store import FeedbackStore  # noqa: E402

STORE_BASE_DIR = Path(os.getenv("RADI_CT_BASE_DIR", str(ROOT))).resolve()


# Назначение: прочитать текст либо из файла, либо из stdin.
# Вход: Path или None. Если путь равен "-", читаем stdin.
# Выход: строка UTF-8.
def read_text_arg(path: str | None) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


# Назначение: разбить feedback.md на пункты.
# Вход: сырой текст, где пункты могут быть bullet-списком или отдельными строками.
# Выход: список непустых строк без ведущих "- ".
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


# Назначение: красиво вывести результат команды для человека и для тестов.
# Вход: словарь с простыми значениями.
# Выход: JSON в stdout.
def print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# Назначение: создать case draft из входного markdown/text-файла.
# Вход: argparse namespace.
# Выход: запись в data/cases/drafts и JSON с case_id/path.
def cmd_draft(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    input_text = read_text_arg(args.input)
    assistant_draft = read_text_arg(args.draft).strip() if args.draft else ""
    record = store.create_case(
        input_text=input_text,
        assistant_draft=assistant_draft,
        task=args.task,
        input_type=args.input_type,
        area=args.area or [],
        clinical_context=args.clinical_context or "",
        comparison=args.comparison,
        references_used=args.reference or [],
    )
    path = store.drafts_dir / f"{record.metadata.case_id}.md"
    print_json(
        {
            "case_id": record.metadata.case_id,
            "status": record.metadata.status,
            "path": str(path),
        }
    )


# Назначение: принять draft без правок.
# Вход: case_id и флаг --save-as-reference.
# Выход: accepted case, feedback event, опционально reference.
def cmd_accept(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    record = store.accept_case(args.case_id, save_as_reference=args.save_as_reference)
    print_json(
        {
            "case_id": record.metadata.case_id,
            "status": record.metadata.status,
            "path": str(store.accepted_dir / f"{record.metadata.case_id}.md"),
            "saved_as_reference": args.save_as_reference,
        }
    )


# Назначение: сохранить исправленный финал Романа и объяснение правок.
# Вход: case_id, --final, --feedback, --tag.
# Выход: corrected case, feedback event, lesson/reference при явных флагах.
def cmd_correct(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    final_text = read_text_arg(args.final)
    feedback = parse_feedback_items(read_text_arg(args.feedback)) if args.feedback else []
    record = store.correct_case(
        args.case_id,
        roman_final=final_text,
        feedback=feedback,
        error_tags=args.tag or [],
        save_as_reference=args.save_as_reference,
        create_lesson_candidate=args.create_lesson_candidate,
    )
    print_json(
        {
            "case_id": record.metadata.case_id,
            "status": record.metadata.status,
            "path": str(store.corrected_dir / f"{record.metadata.case_id}.md"),
            "feedback_items": len(record.feedback),
            "error_tags": record.error_tags,
            "saved_as_reference": args.save_as_reference,
            "lesson_candidate": args.create_lesson_candidate,
        }
    )


# Назначение: явно перенести accepted/corrected case в reference base.
# Вход: case_id.
# Выход: reference markdown в data/references.
def cmd_promote(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    path = store.promote_to_reference(args.case_id)
    print_json({"case_id": args.case_id, "reference_path": str(path)})


# Назначение: показать список cases.
# Вход: опциональный status.
# Выход: JSON-список cases.
def cmd_cases(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    print(json.dumps(store.list_cases(status=args.status), ensure_ascii=False, indent=2))


# Назначение: показать lesson candidates.
# Вход: ничего.
# Выход: JSON-список markdown-файлов кандидатов правил.
def cmd_lessons(args: argparse.Namespace) -> None:
    store = FeedbackStore(base_dir=STORE_BASE_DIR)
    store.ensure_dirs()
    lessons = [str(path) for path in sorted(store.lesson_candidates_dir.glob("*.md"))]
    print(json.dumps(lessons, ensure_ascii=False, indent=2))


# Назначение: заглушка для совместимости с roadmap.
# Вход: ничего.
# Выход: понятная ошибка, потому что index уже реализован отдельным scripts/index_base.py.
def cmd_index(args: argparse.Namespace) -> None:
    raise SystemExit("Use existing command: python scripts/index_base.py")


# Назначение: собрать argparse CLI с подкомандами draft/accept/correct/promote/cases/lessons/index.
# Вход: argv или None.
# Выход: настроенный parser.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadiCT Assistant learning-loop CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft = subparsers.add_parser("draft", help="Create local case draft")
    draft.add_argument("input", help="Input markdown/text file, or '-' for stdin")
    draft.add_argument("--draft", help="Assistant draft file")
    draft.add_argument("--task", default="conclusion", choices=["conclusion", "description", "description_and_conclusion", "edit_description", "edit_conclusion"])
    draft.add_argument("--input-type", default="markdown", choices=["text", "markdown", "voice_transcript"])
    draft.add_argument("--area", action="append", help="Study area, can be repeated")
    draft.add_argument("--clinical-context", default="", help="Anonymized clinical context")
    draft.add_argument("--comparison", action="store_true", help="Dynamic comparison exists")
    draft.add_argument("--reference", action="append", help="Reference path/id used for draft")
    draft.set_defaults(func=cmd_draft)

    accept = subparsers.add_parser("accept", help="Accept case draft")
    accept.add_argument("case_id")
    accept.add_argument("--save-as-reference", action="store_true")
    accept.set_defaults(func=cmd_accept)

    correct = subparsers.add_parser("correct", help="Correct case with Roman final text")
    correct.add_argument("case_id")
    correct.add_argument("--final", required=True, help="Final text file, or '-' for stdin")
    correct.add_argument("--feedback", help="Feedback text file")
    correct.add_argument("--tag", action="append", help="Error tag, can be repeated")
    correct.add_argument("--save-as-reference", action="store_true")
    correct.add_argument("--create-lesson-candidate", action="store_true")
    correct.set_defaults(func=cmd_correct)

    promote = subparsers.add_parser("promote", help="Promote accepted/corrected case to reference base")
    promote.add_argument("case_id")
    promote.set_defaults(func=cmd_promote)

    cases = subparsers.add_parser("cases", help="List cases")
    cases.add_argument("--status", choices=["draft", "accepted", "corrected"])
    cases.set_defaults(func=cmd_cases)

    lessons = subparsers.add_parser("lessons", help="List lesson candidates")
    lessons.set_defaults(func=cmd_lessons)

    index = subparsers.add_parser("index", help="Compatibility wrapper for roadmap")
    index.set_defaults(func=cmd_index)

    return parser


# Назначение: точка входа CLI.
# Вход: argv или None.
# Выход: код процесса 0 при успехе, ошибка argparse/SystemExit при проблемах.
def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
