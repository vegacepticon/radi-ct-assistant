"""
Единая точка выбора RAG backend-а для RadiCT Assistant.

Сейчас поддерживаются:
- obsidian_hybrid — основной backend через obsidian-hybrid-search;
- chroma — старый прототип через ChromaDB/sentence-transformers.

Почему отдельный модуль:
- main.py не должен знать детали конкретного поискового движка;
- тесты learning loop не должны импортировать тяжёлые зависимости;
- backend можно переключать переменной окружения RAG_BACKEND.
"""
from __future__ import annotations

from typing import Any

from .config import RAG_BACKEND


# Назначение: создать retriever по текущему RAG_BACKEND.
# Вход: переменная окружения RAG_BACKEND.
# Выход: объект с методом search(query_description, area, top_k...).
def create_retriever() -> Any:
    backend = RAG_BACKEND.strip().lower()
    if backend in {"obsidian_hybrid", "ohs", "obsidian"}:
        from .ohs import ObsidianHybridRetriever

        return ObsidianHybridRetriever()
    if backend in {"chroma", "chromadb"}:
        from .retriever import Retriever

        return Retriever()
    raise ValueError(f"Unsupported RAG_BACKEND: {RAG_BACKEND}")


# Назначение: переиндексировать активный RAG backend.
# Вход: force=True для полной пересборки, где поддерживается.
# Выход: dict с indexed/message/backend/status.
def reindex_active_backend(force: bool = False) -> dict[str, Any]:
    backend = RAG_BACKEND.strip().lower()
    if backend in {"obsidian_hybrid", "ohs", "obsidian"}:
        from .ohs import ohs_reindex

        status = ohs_reindex(force=force)
        return {
            "backend": "obsidian_hybrid",
            "indexed": status.indexed,
            "chunks": status.chunks,
            "message": f"OHS indexed {status.indexed} notes / {status.chunks} chunks",
        }
    if backend in {"chroma", "chromadb"}:
        from .indexer import Indexer

        count = Indexer().index_directory()
        return {"backend": "chroma", "indexed": count, "message": f"Проиндексировано {count} записей"}
    raise ValueError(f"Unsupported RAG_BACKEND: {RAG_BACKEND}")


# Назначение: вернуть readiness/status активного RAG backend-а.
# Вход: ничего.
# Выход: dict для /api/rag/status.
def rag_status() -> dict[str, Any]:
    backend = RAG_BACKEND.strip().lower()
    if backend in {"obsidian_hybrid", "ohs", "obsidian"}:
        from .ohs import ohs_status

        status = ohs_status()
        return {
            "backend": status.backend,
            "available": status.available,
            "command": status.command,
            "vault": status.vault,
            "total": status.total,
            "indexed": status.indexed,
            "chunks": status.chunks,
            "model": status.model,
            "version": status.version,
            "error": status.error,
        }
    if backend in {"chroma", "chromadb"}:
        try:
            from .indexer import Indexer

            stats = Indexer().get_stats()
            return {"backend": "chroma", "available": True, **stats}
        except Exception as e:
            return {"backend": "chroma", "available": False, "error": str(e)}
    return {"backend": RAG_BACKEND, "available": False, "error": "unsupported backend"}
