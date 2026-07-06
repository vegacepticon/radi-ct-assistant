"""
FastAPI сервис RadiCT Assistant.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .prompt_builder import build_prompt
from .llm_client import generate
from .parser import parse_directory
from .config import BASE_DIR, REFERENCES_DIR
from .case_schema import InputType, TaskName
from .feedback_store import FeedbackStore

app = FastAPI(title="RadiCT Assistant", version="0.1.0")

# Глобальные экземпляры (ленивая инициализация)
_indexer: Any | None = None
_retriever: Any | None = None


# Назначение: получить локальное хранилище learning loop.
# Вход: переменная окружения RADI_CT_BASE_DIR для тестов или пусто для обычной работы.
# Выход: FeedbackStore, который пишет в data/cases и data/feedback внутри base_dir.
def get_feedback_store() -> FeedbackStore:
    base_dir = Path(os.getenv("RADI_CT_BASE_DIR", str(BASE_DIR))).resolve()
    return FeedbackStore(base_dir=base_dir)


# Назначение: лениво создать embedding indexer.
# Вход: ничего.
# Выход: singleton Indexer для переиндексации reference base.
def get_indexer() -> Any:
    global _indexer
    if _indexer is None:
        from .indexer import Indexer

        _indexer = Indexer()
    return _indexer


# Назначение: лениво создать retriever.
# Вход: ничего.
# Выход: singleton Retriever для поиска few-shot примеров.
def get_retriever() -> Any:
    global _retriever
    if _retriever is None:
        from .retriever import Retriever

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


class DraftRequest(BaseModel):
    input_text: str = Field(..., description="Входной текст: описание, черновые находки или markdown")
    task: TaskName = Field("conclusion", description="Задача learning loop")
    input_type: InputType = Field("markdown", description="text / markdown / voice_transcript")
    area: list[str] = Field(default_factory=list, description="Области исследования")
    clinical_context: str = Field("", description="Обезличенный клинический контекст")
    comparison: bool = Field(False, description="Есть ли сравнение в динамике")
    mode: str = Field("fast", description="Режим генерации для старого generate path")
    assistant_draft: str = Field("", description="Готовый черновик; если передан, LLM не вызывается")


class DraftResponse(BaseModel):
    case_id: str
    draft: str
    references_used: list[str] = []
    path: str


class AcceptRequest(BaseModel):
    save_as_reference: bool = False


class CaseActionResponse(BaseModel):
    case_id: str
    status: str
    path: str
    saved_as_reference: bool = False


class CorrectRequest(BaseModel):
    roman_final: str = Field(..., description="Финальный вариант Романа")
    feedback: str | list[str] = Field(default_factory=list, description="Объяснение правок")
    error_tags: list[str] = Field(default_factory=list)
    save_as_reference: bool = False
    create_lesson_candidate: bool = False


class LessonInfo(BaseModel):
    path: str
    content: str


class CaseSummary(BaseModel):
    case_id: str
    status: str
    task: str
    area: list[str]
    created_at: str
    path: str


class CaseDetail(BaseModel):
    case_id: str
    created_at: str
    task: str
    input_type: str
    area: list[str]
    clinical_context: str
    comparison: bool
    status: str
    references_used: list[str]
    input_text: str
    assistant_draft: str
    roman_final: str
    feedback: list[str]
    error_tags: list[str]


class PromoteResponse(BaseModel):
    case_id: str
    reference_path: str


# --- Helpers ---

# Назначение: нормализовать feedback из строки или массива строк.
# Вход: feedback как "- пункт\n- пункт" или ["пункт", "пункт"].
# Выход: список непустых пунктов без markdown-маркера "- ".
def _normalize_feedback(feedback: str | list[str]) -> list[str]:
    if isinstance(feedback, list):
        return [item.strip() for item in feedback if item.strip()]

    items: list[str] = []
    for line in feedback.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        items.append(line)
    return items


# Назначение: преобразовать внутренний CaseRecord в API DTO.
# Вход: CaseRecord из FeedbackStore.load_case().
# Выход: CaseDetail без дополнительных чтений с диска.
def _case_detail_from_record(record: Any) -> CaseDetail:
    return CaseDetail(
        case_id=record.metadata.case_id,
        created_at=record.metadata.created_at,
        task=record.metadata.task,
        input_type=record.metadata.input_type,
        area=record.metadata.area,
        clinical_context=record.metadata.clinical_context,
        comparison=record.metadata.comparison,
        status=record.metadata.status,
        references_used=record.metadata.references_used,
        input_text=record.input_text,
        assistant_draft=record.assistant_draft,
        roman_final=record.roman_final,
        feedback=record.feedback,
        error_tags=record.error_tags,
    )


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


@app.post("/api/draft", response_model=DraftResponse)
async def create_draft(req: DraftRequest):
    """Создаёт learning-loop draft case и возвращает case_id.

    Если assistant_draft уже передан, endpoint только сохраняет case и не
    вызывает retrieval/LLM. Это основной безопасный путь для локальных тестов.
    Если assistant_draft пустой, используется текущий generate path.
    """
    if not req.input_text.strip():
        raise HTTPException(400, "input_text не может быть пустым")

    references_used: list[str] = []
    draft = req.assistant_draft.strip()

    if not draft:
        area = req.area[0] if req.area else ""
        generated = await generate_conclusion(
            GenerateRequest(
                description=req.input_text,
                area=area,
                mode=req.mode,
                clinical_context=req.clinical_context,
            )
        )
        draft = generated.conclusion
        references_used = generated.references_used

    store = get_feedback_store()
    record = store.create_case(
        input_text=req.input_text,
        assistant_draft=draft,
        task=req.task,
        input_type=req.input_type,
        area=req.area,
        clinical_context=req.clinical_context,
        comparison=req.comparison,
        references_used=references_used,
    )
    return DraftResponse(
        case_id=record.metadata.case_id,
        draft=record.assistant_draft,
        references_used=record.metadata.references_used,
        path=str(store.drafts_dir / f"{record.metadata.case_id}.md"),
    )


@app.post("/api/accept/{case_id}", response_model=CaseActionResponse)
async def accept_case(case_id: str, req: AcceptRequest):
    """Принимает draft без правок и пишет feedback event."""
    store = get_feedback_store()
    try:
        record = store.accept_case(case_id, save_as_reference=req.save_as_reference)
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    return CaseActionResponse(
        case_id=record.metadata.case_id,
        status=record.metadata.status,
        path=str(store.accepted_dir / f"{record.metadata.case_id}.md"),
        saved_as_reference=req.save_as_reference,
    )


@app.post("/api/correct/{case_id}", response_model=CaseActionResponse)
async def correct_case(case_id: str, req: CorrectRequest):
    """Сохраняет финальную правку Романа, feedback и error tags."""
    if not req.roman_final.strip():
        raise HTTPException(400, "roman_final не может быть пустым")

    store = get_feedback_store()
    try:
        record = store.correct_case(
            case_id,
            roman_final=req.roman_final,
            feedback=_normalize_feedback(req.feedback),
            error_tags=req.error_tags,
            save_as_reference=req.save_as_reference,
            create_lesson_candidate=req.create_lesson_candidate,
        )
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    return CaseActionResponse(
        case_id=record.metadata.case_id,
        status=record.metadata.status,
        path=str(store.corrected_dir / f"{record.metadata.case_id}.md"),
        saved_as_reference=req.save_as_reference,
    )


@app.get("/api/lessons", response_model=list[LessonInfo])
async def list_lessons():
    """Показывает candidates правил, ещё не перенесённые в style skill."""
    store = get_feedback_store()
    store.ensure_dirs()
    return [
        LessonInfo(path=str(path), content=path.read_text(encoding="utf-8"))
        for path in sorted(store.lesson_candidates_dir.glob("*.md"))
    ]


@app.get("/api/cases", response_model=list[CaseSummary])
async def list_cases(status: str | None = None):
    """Показывает список локальных learning-loop cases."""
    if status is not None and status not in {"draft", "accepted", "corrected"}:
        raise HTTPException(400, "status must be one of: draft, accepted, corrected")

    store = get_feedback_store()
    return [CaseSummary(**item) for item in store.list_cases(status=status)]


@app.get("/api/cases/{case_id}", response_model=CaseDetail)
async def get_case(case_id: str):
    """Возвращает полный локальный case: input, draft, final, feedback, tags."""
    store = get_feedback_store()
    try:
        record = store.load_case(case_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    return _case_detail_from_record(record)


@app.post("/api/references/promote/{case_id}", response_model=PromoteResponse)
async def promote_case_to_reference(case_id: str):
    """Явно переносит accepted/corrected case в few-shot reference base.

    Promotion может упасть с 400, если case ещё draft, нет финального текста
    или базовый PHI guard нашёл прямой идентификатор.
    """
    store = get_feedback_store()
    try:
        path = store.promote_to_reference(case_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return PromoteResponse(case_id=case_id, reference_path=str(path))


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
