"""
Индексация базы референсов через embeddings.
Хранит векторы в ChromaDB для быстрого similarity search.
"""
import hashlib
from pathlib import Path
import chromadb
from sentence_transformers import SentenceTransformer

from .parser import ReferenceEntry, parse_directory
from .config import EMBEDDING_MODEL, INDEX_DIR, REFERENCES_DIR


def _doc_id(filepath: str) -> str:
    """Стабильный ID для ChromaDB на основе пути."""
    return hashlib.md5(filepath.encode()).hexdigest()


class Indexer:
    """Индексатор базы референсов."""

    def __init__(self):
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.client = chromadb.PersistentClient(path=str(INDEX_DIR))
        self.collection = self.client.get_or_create_collection(
            name="references",
            metadata={"hnsw:space": "cosine"},
        )

    def index_directory(self, directory: Path = None) -> int:
        """
        Индексирует все качественные .md файлы.
        Возвращает количество проиндексированных записей.
        """
        directory = directory or REFERENCES_DIR
        entries = parse_directory(directory)

        if not entries:
            print(f"[indexer] No quality entries found in {directory}")
            return 0

        # Очищаем старую коллекцию и переиндексируем
        self.client.delete_collection("references")
        self.collection = self.client.get_or_create_collection(
            name="references",
            metadata={"hnsw:space": "cosine"},
        )

        for entry in entries:
            self._add_entry(entry)

        print(f"[indexer] Indexed {len(entries)} entries")
        return len(entries)

    def _add_entry(self, entry: ReferenceEntry):
        """Добавляет одну запись в индекс."""
        doc_id = _doc_id(entry.filepath)
        embedding = self.model.encode(entry.description, normalize_embeddings=True)

        self.collection.add(
            ids=[doc_id],
            embeddings=[embedding.tolist()],
            documents=[entry.description],
            metadatas=[{
                "filepath": entry.filepath,
                "area": entry.area,
                "doctor": entry.doctor,
                "conclusion": entry.conclusion,
                "recommendation": entry.recommendation,
            }],
        )

    def add_single(self, entry: ReferenceEntry):
        """Добавляет или обновляет одну запись (для watchdog)."""
        doc_id = _doc_id(entry.filepath)
        # Удаляем старую версию если есть
        try:
            self.collection.delete(ids=[doc_id])
        except Exception:
            pass
        self._add_entry(entry)

    def get_stats(self) -> dict:
        """Статистика индекса."""
        count = self.collection.count()
        return {"total_entries": count}