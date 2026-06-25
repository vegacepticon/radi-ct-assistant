#!/usr/bin/env python3
"""
Templater script: очистка YAML frontmatter текущей заметки от PHI.

Скрипт ONLY чистит YAML содержимое файла (in-place).
Переименование файла делает Templater через tp.file.rename().

Вариант 1 — User System Command (Templater Settings → User System Command Functions):
    Name: scrub_phi
    Command: python3 "/path/to/scrub_phi_templater.py" "<% tp.file.path() %>"
    Template: <%* await tp.user.scrub_phi(); await tp.file.rename(String(Math.floor(Math.random() * 9000000) + 100000)); %>

Вариант 2 — через require('child_process') в <%*> блоке (без настроек):
    <%*
    const { execSync } = require('child_process');
    const filePath = tp.file.path();  // абсолютный путь по умолчанию
    execSync(`python3 "/path/to/scrub_phi_templater.py" "${filePath}"`);
    await tp.file.rename(String(Math.floor(Math.random() * 9000000) + 100000));
    %>

Скрипт:
1. Читает текущий файл
2. Оставляет в YAML только: анамнез, область, сравнение, экстренность, статус
3. Перезаписывает файл на месте (content only, без переименования)

Требуется: pip install pyyaml
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


KEY_ORDER = ["анамнез", "область", "сравнение", "экстренность", "статус"]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if match:
        raw_yaml = match.group(1)
        body = match.group(2)
        try:
            metadata = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata, body
    return {}, text


def scrub_metadata(metadata: dict) -> dict:
    return {k: metadata[k] for k in KEY_ORDER if k in metadata}


def build_frontmatter(metadata: dict) -> str:
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


def scrub_inplace(filepath: Path) -> None:
    """Очищает YAML frontmatter файла на месте. Не переименовывает."""
    text = filepath.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    cleaned_meta = scrub_metadata(metadata)
    frontmatter_str = build_frontmatter(cleaned_meta)
    output_text = frontmatter_str + body
    filepath.write_text(output_text, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: scrub_phi_templater.py <path_to_note.md>", file=sys.stderr)
        sys.exit(1)

    note_path = Path(sys.argv[1])
    if not note_path.exists():
        print(f"ERROR: File not found: {note_path}", file=sys.stderr)
        sys.exit(1)

    scrub_inplace(note_path)
    print(f"Scrubbed: {note_path.name}")