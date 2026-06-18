"""
Скрипт ручной индексации базы референсов.

Usage:
    python scripts/index_base.py
    python scripts/index_base.py --dir /path/to/references
"""
import argparse
import sys
from pathlib import Path

# Добавляем src/ в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.indexer import Indexer
from src.config import REFERENCES_DIR


def main():
    parser = argparse.ArgumentParser(description="Индексация базы референсов")
    parser.add_argument("--dir", default=str(REFERENCES_DIR),
                        help="Папка с .md файлами")
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.exists():
        print(f"Directory not found: {directory}")
        sys.exit(1)

    print(f"Indexing references from: {directory}")
    indexer = Indexer()
    count = indexer.index_directory(directory)
    print(f"Done: {count} entries indexed")


if __name__ == "__main__":
    main()