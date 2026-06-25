#!/usr/bin/env python3
"""
Очистка .md файлов протоколов КТ от конфиденциальных данных (PHI).

Оставляет в YAML frontmatter только: анамнез, область, сравнение, экстренность, статус.
Все остальные поля удаляются.
Файл переименовывается в <случайное_число>.md (полная анонимизация).

Usage:
    python scripts/scrub_phi.py --input /path/to/raw --output data/references/clean
    python scripts/scrub_phi.py --input /path/to/file.md --output data/references/clean
"""
import argparse
import random
import re
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML required: pip install pyyaml")


# Поля YAML, которые ОСТАВЛЯЕМ (всё остальное удаляется)
KEEP_KEYS = {"анамнез", "область", "сравнение", "экстренность", "статус"}


def parse_frontmatter(text: str) -> tuple[dict, str, str]:
    """
    Разделяет файл на (metadata_dict, body, frontmatter_delimiter).
    Возвращает (metadata, body, raw_yaml_or_empty).
    """
    # Поддержка --- и +++ (Obsidian использует ---)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if match:
        raw_yaml = match.group(1)
        body = match.group(2)
        try:
            metadata = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata, body, raw_yaml

    return {}, text, ""


# Канонический порядок ключей в выводе
KEY_ORDER = ["анамнез", "область", "сравнение", "экстренность", "статус"]


def scrub_metadata(metadata: dict) -> dict:
    """Оставляет только разрешённые ключи в каноническом порядке."""
    cleaned = {}
    for key in KEY_ORDER:
        if key in metadata:
            cleaned[key] = metadata[key]
    return cleaned


def build_frontmatter(metadata: dict) -> str:
    """Собирает YAML frontmatter из dict."""
    if not metadata:
        return ""
    yaml_text = yaml.dump(
        metadata,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )
    return f"---\n{yaml_text}---\n\n"


def generate_filename(directory: Path) -> Path:
    """
    Генерирует путь с случайным числом-именем.
    Проверяет коллизии.
    """
    while True:
        num = random.randint(100000, 9999999)
        candidate = directory / f"{num}.md"
        if not candidate.exists():
            return candidate


def scrub_file(filepath: Path, output_dir: Path | None = None, inplace: bool = False) -> bool:
    """
    Очищает один файл.
    inplace=True — перезаписывает исходный файл (для Templater сценария).
    inplace=False — создаёт новый файл в output_dir со случайным именем.
    """
    try:
        text = filepath.read_text(encoding="utf-8")
        metadata, body, _ = parse_frontmatter(text)
        cleaned_meta = scrub_metadata(metadata)
        frontmatter_str = build_frontmatter(cleaned_meta)

        output_text = frontmatter_str + body

        if inplace:
            output_path = filepath
            output_path.write_text(output_text, encoding="utf-8")
        else:
            if output_dir is None:
                raise ValueError("output_dir required when inplace=False")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = generate_filename(output_dir)
            output_path.write_text(output_text, encoding="utf-8")

        action = "scrubbed (inplace)" if inplace else f"→ {output_path.name}"
        print(f"  [OK] {filepath.name} {action}")
        return True

    except Exception as e:
        print(f"  [ERROR] {filepath.name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Очистка .md файлов протоколов КТ от PHI"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Файл .md или папка с исходными файлами",
    )
    parser.add_argument(
        "--output",
        default="data/references/clean",
        help="Папка для очищенных файлов (по умолчанию data/references/clean)",
    )
    parser.add_argument(
        "--inplace",
        action="store_true",
        help="Перезаписать исходный файл (не создаёт копию)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.is_file():
        files = [input_path]
        if not args.inplace:
            output_dir = Path(args.output)
        else:
            output_dir = None
    elif input_path.is_dir():
        files = sorted(input_path.rglob("*.md"))
        if not args.inplace:
            output_dir = Path(args.output)
        else:
            output_dir = None
    else:
        print(f"Input not found: {input_path}")
        return

    if not args.inplace:
        print(f"Input: {input_path}")
        print(f"Output: {output_dir}")
    else:
        print(f"Mode: inplace (modifying original files)")
    print(f"Files: {len(files)}")
    print()

    cleaned = 0
    for filepath in files:
        if scrub_file(filepath, output_dir, inplace=args.inplace):
            cleaned += 1

    print(f"\nDone: {cleaned}/{len(files)} files processed")


if __name__ == "__main__":
    main()