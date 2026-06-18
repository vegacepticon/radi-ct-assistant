"""
FastAPI сервис RadiCT Assistant.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .indexer import Indexer
from .retriever import Retriever
from .prompt_builder import build_prompt
from .llm_client import generate
from .parser import parse_file, parse_directory
from .config import REFERENCES_DIR

app = FastAPI(title="RadiCT Assistant", version="0.1.0")

# Глобальные экземпляры (ленивая инициализация)
_indexer: Indexer | None = None
_retriever: Retriever | None = None


def get_indexer() -> Indexer:
    global _indexer
    if _indexer is None:
        _indexer = Indexer()
    return _indexer


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever


# --- Models ---

class GenerateRequest(BaseModel):
    description: str = Field(..., description="Описательная часть КТ-исследования")
    area: str = Field("", description="Область исследования, e.g. 'КТА ГМ'")
    mode: str = Field("fast", description="Режим: 'fast' или 'analytical'")
    clinical_context: str = Field("", description="Возраст, пол, клиническая задача")


class GenerateResponse(BaseModel):
    conclusion: str
    differential: str | None = None
    references_used: list[str] = []


class ReindexResponse(BaseModel):
    indexed: int
    message: str


class ReferenceInfo(BaseModel):
    filepath: str
    area: str
    doctor: str
    date: str


# --- Endpoints ---

@app.post("/api/generate", response_model=GenerateResponse)
async def generate_conclusion(req: GenerateRequest):
    """Генерирует заключение КТ на основе описания."""
    if not req.description.strip():
        raise HTTPException(400, "Описание не может быть пустым")

    if req.mode not in ("fast", "analytical"):
        raise HTTPException(400, "Режим должен быть 'fast' или 'analytical'")

    retriever = get_retriever()
    references = retriever.search(
        query_description=req.description,
        area=req.area,
    )

    prompt = build_prompt(
        description=req.description,
        references=references,
        mode=req.mode,
        clinical_context=req.clinical_context,
    )

    try:
        raw_output = await generate(prompt)
    except Exception as e:
        raise HTTPException(502, f"LLM API error: {e}")

    # Разделяем заключение и диффдиагноз (если есть)
    conclusion = raw_output
    differential = None
    if "---" in raw_output and req.mode == "analytical":
        parts = raw_output.split("---", 1)
        conclusion = parts[0].strip()
        differential = parts[1].strip() if len(parts) > 1 else None

    ref_paths = [r.description[:80] + "..." for r in references]

    return GenerateResponse(
        conclusion=conclusion,
        differential=differential,
        references_used=ref_paths,
    )


@app.post("/api/reindex", response_model=ReindexResponse)
async def reindex():
    """Перестраивает векторный индекс из базы референсов."""
    indexer = get_indexer()
    count = indexer.index_directory()
    return ReindexResponse(
        indexed=count,
        message=f"Проиндексировано {count} записей",
    )


@app.get("/api/references")
async def list_references():
    """Список всех референсов в базе."""
    entries = parse_directory(REFERENCES_DIR)
    return [
        ReferenceInfo(
            filepath=e.filepath,
            area=e.area,
            doctor=e.doctor,
            date=str(e.metadata.get("дата", "")),
        )
        for e in entries
    ]


@app.get("/api/health")
async def health():
    """Проверка работоспособности."""
    return {"status": "ok", "version": "0.1.0"}