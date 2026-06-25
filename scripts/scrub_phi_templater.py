#!/usr/bin/env python3
"""
Templater script: очистка YAML frontmatter текущей заметки от PHI.

Вариант 1 — через tp.system.command (простой):
    <% tp.system.command("python3 'C:/path/to/scrub_phi_templater.py' '" + tp.file.path(true) + "'") %>

Вариант 2 — через tp.user функцию (если настроен пользовательский скрипт):
    <% tp.user.scrub_phi(tp.file.path(true)) %>

Вариант 3 — через запуск файла (Templater > User Scripts):
    Поместить этот файл в папку Templater user scripts, затем:
    <% tp.user.scrub_phi_templater(tp.file.path(true)) %>

Скрипт:
1. Читает текущий файл
2. Оставляет в YAML только: анамнез, область, сравнение, экстренность, статус
3. Переименовывает файл в <случайное_число>.md
4. Перезаписывает содержимое

Важно: tp.file.path(true) возвращает абсолютный путь.
Без аргументов — ничего не делает (защита от случайного вызова).
"""
import random
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)


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


KEY_ORDER = ["анамнез", "область", "сравнение", "экстренность", "статус"]


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


def scrub_inplace(filepath: Path) -> Path:
    """
    Очищает файл на месте и переименовывает в <случайное_число>.md.
    Возвращает новый путь.
    """
    text = filepath.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    cleaned_meta = scrub_metadata(metadata)
    frontmatter_str = build_frontmatter(cleaned_meta)
    output_text = frontmatter_str + body

    # Новое имя — случайное число
    directory = filepath.parent
    while True:
        num = random.randint(100000, 9999999)
        new_path = directory / f"{num}.md"
        if not new_path.exists() or new_path == filepath:
            break

    # Пишем очищенный контент во временный файл, затем переименовываем
    temp_path = directory / f".tmp_scrub_{num}.md"
    temp_path.write_text(output_text, encoding="utf-8")
    
    # Удаляем оригинал и переименовываем temp
    if filepath.exists():
        filepath.unlink()
    temp_path.rename(new_path)
    
    return new_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: scrub_phi_templater.py <path_to_note.md>")
        sys.exit(1)
    
    note_path = Path(sys.argv[1])
    if not note_path.exists():
        print(f"ERROR: File not found: {note_path}")
        sys.exit(1)
    
    new_path = scrub_inplace(note_path)
    print(f"Scrubbed: {note_path.name} → {new_path.name}")
    print(f"Path: {new_path}")