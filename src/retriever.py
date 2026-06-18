"""
Retrieval: поиск похожих описаний в векторном индексе.
"""
from dataclasses import dataclass
from sentence_transformers import SentenceTransformer
import chromadb

from .config import EMBEDDING_MODEL, INDEX_DIR, TOP_K, MIN_SIMILARITY


@dataclass
class RetrievalResult:
    """Результат поиска одного референса."""
    description: str
    conclusion: str
    recommendation: str
    similarity: float
    area: str
    doctor: str


class Retriever:
    """Поиск релевантных few-shot примеров."""

    def __init__(self):
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.client = chromadb.PersistentClient(path=str(INDEX_DIR))
        self.collection = self.client.get_or_create_collection(
            name="references",
            metadata={"hnsw:space": "cosine"},
        )

    def search(
        self,
        query_description: str,
        area: str = "",
        top_k: int = TOP_K,
        min_similarity: float = MIN_SIMILARITY,
    ) -> list[RetrievalResult]:
        """
        Ищет похожие описания в базе.
        Фильтрует по области исследования если задана.
        """
        query_embedding = self.model.encode(
            query_description, normalize_embeddings=True
        )

        where_filter = None
        if area:
            where_filter = {"area": area}

        results = self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=top_k * 2,  # берём с запасом, потом фильтруем
            where=where_filter,
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        # Если фильтр по области ничего не дал — ищем без фильтра
        if not results["ids"][0] and area:
            results = self.collection.query(
                query_embeddings=[query_embedding.tolist()],
                n_results=top_k,
            )

        retrieval_results = []
        for i, doc_id in enumerate(results["ids"][0]):
            sim = 1.0 - results["distances"][0][i]  # cosine distance → similarity
            if sim < min_similarity:
                continue

            meta = results["metadatas"][0][i]
            retrieval_results.append(RetrievalResult(
                description=results["documents"][0][i],
                conclusion=meta.get("conclusion", ""),
                recommendation=meta.get("recommendation", ""),
                similarity=sim,
                area=meta.get("area", ""),
                doctor=meta.get("doctor", ""),
            ))

            if len(retrieval_results) >= top_k:
                break

        return retrieval_results