#!/usr/bin/env python3
"""
CLI для аудита reference-файлов RadiCT Assistant.

Проверяет все .md файлы в reference-vault и выводит отчёт:
- человекочитаемый (по умолчанию);
- JSON (--json).

Не мутирует файлы — только read-only аудит.

Использование:
    python3 scripts/audit_references.py
    python3 scripts/audit_references.py --json
    python3 scripts/audit_references.py --vault /path/to/vault
"""
import argparse
import json
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта src.*
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.reference_validator import validate_directory, Severity


def format_severity(sev: Severity) -> str:
    """Цветовое форматирование уровня серьёзности для терминала."""
    colors = {
        Severity.ERROR: "\033[91m",   # красный
        Severity.WARNING: "\033[93m",  # жёлтый
        Severity.INFO: "\033[96m",    # голубой
    }
    reset = "\033[0m"
    return f"{colors.get(sev, '')}{sev.value.upper()}{reset}"


def print_human_report(report_dict: dict) -> None:
    """Вывести человекочитаемый отчёт."""
    print("=" * 70)
    print("RadiCT Reference Audit Report")
    print("=" * 70)
    print()
    print(f"Total references:    {report_dict['total']}")
    print(f"Valid (no errors):   {report_dict['valid']}")
    print(f"With errors:         {report_dict['errors']}")
    print(f"With warnings:       {report_dict['warnings']}")
    print(f"Synthetic:           {report_dict['synthetic']}")
    print(f"Needs review:        {report_dict['needs_review']}")
    print()
    print("-" * 70)
    print()

    for ref in report_dict["references"]:
        status_icon = "✅" if ref["is_valid"] else "❌"
        action = ref["recommended_action"]
        print(f"{status_icon} {ref['reference_id']}")
        print(f"   task: {ref['task'] or '—'}")
        print(f"   areas: {ref['areas'] or '—'}")
        print(f"   status: {ref['reference_status']} | quality: {ref['quality']}")
        print(f"   body: {ref['body_length']} chars | synthetic: {ref['is_synthetic']}")
        print(f"   recommended: {action}")

        if ref["issues"]:
            for issue in ref["issues"]:
                sev_str = issue["severity"].upper()
                print(f"   [{sev_str}] {issue['code']}: {issue['message']}")
                if issue["detail"]:
                    print(f"      {issue['detail']}")
        print()

    # Сводка рекомендаций
    print("=" * 70)
    print("SUMMARY OF RECOMMENDED ACTIONS")
    print("=" * 70)
    actions = {}
    for ref in report_dict["references"]:
        action = ref["recommended_action"]
        actions.setdefault(action, []).append(ref["reference_id"])

    for action, ids in sorted(actions.items()):
        print(f"\n{action} ({len(ids)}):")
        for rid in ids:
            print(f"  - {rid}")

    # Ненулевой exit code если есть errors
    if report_dict["errors"] > 0:
        print("\n⚠️  Audit completed with ERRORS — some references need attention.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit RadiCT reference files (read-only, no mutations)"
    )
    parser.add_argument(
        "--vault",
        default=str(project_root / "data" / "reference-vault"),
        help="Путь к reference vault (по умолчанию: data/reference-vault)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести отчёт в JSON формате",
    )
    args = parser.parse_args()

    vault_path = Path(args.vault)
    if not vault_path.exists():
        print(f"Error: vault directory not found: {vault_path}", file=sys.stderr)
        sys.exit(2)

    report = validate_directory(vault_path)
    report_dict = report.to_dict()

    if args.json:
        print(json.dumps(report_dict, ensure_ascii=False, indent=2))
    else:
        print_human_report(report_dict)


if __name__ == "__main__":
    main()