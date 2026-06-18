"""
Скрипт очистки .md файлов базы от конфиденциальных данных (PHI).

Удаляет:
- id: поле из YAML frontmatter
- Имена пациентов (если есть в теле)
- Даты рождения
- Любые другие PHI поля

Сохраняет структуру и медицинский контент.
Вывод в output_dir (по умолчанию data/references/clean/).

Usage:
    python scripts/scrub_phi.py --input /path/to/raw --output data/references/clean
"""
import argparse
import re
from pathlib import Path
import frontmatter


# YAML поля для удаления
PHI_YAML_KEYS = {"id", "пациент", "фио", "имя", "датарождения", "дата_рождения"}

# Паттерны в теле для удаления/замены
PHI_BODY_PATTERNS = [
    # Имена в формате "Фамилия И.О." или "Фамилия Имя Отчество"
    (re.compile(r"\b[А-ЯЁ][а-яё]+ [А-ЯЁ]\.[А-ЯЁ]\.(?:\s|$)"), "[ФИО]"),
    # Даты рождения
    (re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b"), "[дата]"),
    # Номера историй болезни
    (re.compile(r"(?:история болезни|ИБ|№)\s*:?\s*\d+", re.IGNORECASE), "[ИБ]"),
]


def scrub_metadata(metadata: dict) -> dict:
    """Удаляет PHI поля из YAML frontmatter."""
    cleaned = {}
    for key, value in metadata.items():
        key_lower = key.lower().replace("_", "").replace("-", "")
        if key_lower in PHI_YAML_KEYS:
            continue
        cleaned[key] = value
    return cleaned


def scrub_body(text: str) -> str:
    """Удаляет PHI из тела документа."""
    for pattern, replacement in PHI_BODY_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_file(filepath: Path, output_dir: Path) -> bool:
    """Очищает один файл. Возвращает True если успешно."""
    try:
        post = frontmatter.load(filepath)
        post.metadata = scrub_metadata(dict(post.metadata))
        post.content = scrub_body(post.content)

        output_path = output_dir / filepath.name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return True
    except Exception as e:
        print(f"  [ERROR] {filepath.name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Очистка .md файлов от PHI")
    parser.add_argument("--input", required=True, help="Папка с исходными файлами")
    parser.add_argument("--output", default="data/references/clean",
                        help="Папка для очищенных файлов")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)

    if not input_dir.exists():
        print(f"Input directory not found: {input_dir}")
        return

    files = list(input_dir.rglob("*.md"))
    print(f"Found {len(files)} .md files")
    print(f"Output: {output_dir}")

    cleaned = 0
    for filepath in sorted(files):
        if scrub_file(filepath, output_dir):
            cleaned += 1

    print(f"\nDone: {cleaned}/{len(files)} files cleaned")


if __name__ == "__main__":
    main()