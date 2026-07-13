#!/usr/bin/env python3
"""
Synthetic guardrail check для RadiCT Assistant.

Проверяет, что в production reference-vault нет синтетических references
со статусом active/gold. Запускается как часть audit pipeline.

Использование:
    python3 scripts/check_synthetic_guardrail.py
    python3 scripts/check_synthetic_guardrail.py --json
"""
import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.reference_validator import validate_directory, Severity


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check for synthetic references in production corpus"
    )
    parser.add_argument(
        "--vault",
        default=str(project_root / "data" / "reference-vault"),
        help="Path to reference vault",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    vault_path = Path(args.vault)
    if not vault_path.exists():
        print(f"Error: vault not found: {vault_path}", file=sys.stderr)
        sys.exit(2)

    report = validate_directory(vault_path)
    report_dict = report.to_dict()

    # Filter: only synthetic references that are active/gold
    synthetic_in_prod = [
        ref for ref in report_dict["references"]
        if ref["is_synthetic"] and ref["reference_status"] in ("active", "gold")
    ]

    result = {
        "checked": report_dict["total"],
        "synthetic_in_production": len(synthetic_in_prod),
        "violations": [
            {
                "reference_id": ref["reference_id"],
                "status": ref["reference_status"],
                "path": ref["path"],
            }
            for ref in synthetic_in_prod
        ],
        "all_synthetic": [
            ref["reference_id"]
            for ref in report_dict["references"]
            if ref["is_synthetic"]
        ],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Synthetic guardrail check")
        print(f"  Checked: {result['checked']} references")
        print(f"  Synthetic in production: {result['synthetic_in_production']}")
        if result["violations"]:
            print(f"  ⚠️  VIOLATIONS:")
            for v in result["violations"]:
                print(f"    - {v['reference_id']} (status={v['status']})")
        else:
            print(f"  ✅ No synthetic references in production corpus")
        if result["all_synthetic"]:
            print(f"  All synthetic (any status): {result['all_synthetic']}")

    if result["synthetic_in_production"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()