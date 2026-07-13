"""
FastAPI сервис RadiCT Assistant.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .parser import parse_directory
from .config import BASE_DIR, RAG_BACKEND, REFERENCE_VAULT_DIR, REFERENCES_DIR
from .case_schema import InputType, TaskName
from .feedback_store import FeedbackStore
from .session_state import SessionStateStore

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


# Назначение: получить session-safe store для связи Hermes session → case_id.
# Вход: переменная окружения RADI_CT_BASE_DIR.
# Выход: SessionStateStore.
def get_session_state_store() -> SessionStateStore:
    base_dir = Path(os.getenv("RADI_CT_BASE_DIR", str(BASE_DIR))).resolve()
    return SessionStateStore(base_dir=base_dir)


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
        from .rag import create_retriever

        _retriever = create_retriever()
    return _retriever


# --- Models ---

class ReindexResponse(BaseModel):
    indexed: int
    message: str
    backend: str = ""
    chunks: int = 0

class RagStatusResponse(BaseModel):
    backend: str
    available: bool
    command: str = ""
    vault: str = ""
    total: int = 0
    indexed: int = 0
    chunks: int = 0
    model: str = ""
    version: str = ""
    error: str = ""


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
    mode: str = Field("fast", description="Режим Hermes-черновика: сохраняется как metadata для аудита")
    assistant_draft: str = Field(..., description="Готовый черновик, сформированный Hermes; backend сам не генерирует текст")
    references_used: list[str] = Field(default_factory=list, description="Reference paths/ids used by Hermes while drafting")


class RagContextRequest(BaseModel):
    input_text: str = Field(..., description="Входной текст для поиска похожих reference-примеров")
    task: TaskName = Field("conclusion", description="conclusion / description / description_and_conclusion")
    area: list[str] = Field(default_factory=list, description="Области исследования; для фильтра используется первая")
    clinical_context: str = Field("", description="Обезличенный клинический контекст")
    mode: str = Field("fast", description="fast / analytical prompt mode")
    top_k: int = Field(5, ge=1, le=10, description="Сколько reference examples вернуть")


class RagReferenceInfo(BaseModel):
    filepath: str
    title: str = ""
    area: str = ""
    similarity: float = 0.0


class RagContextResponse(BaseModel):
    prompt: str
    references_used: list[str]
    references: list[RagReferenceInfo]


class PrepareRequest(BaseModel):
    """Единая operational entry point: prepare = parse + RAG + metadata."""

    input_text: str = Field(..., description="Входной текст: описание, находки или markdown")
    task: TaskName = Field("conclusion", description="conclusion / description / description_and_conclusion")
    area: list[str] = Field(default_factory=list)
    clinical_context: str = Field("")
    comparison: bool = False
    mode: str = Field("fast")
    top_k: int = Field(5, ge=1, le=10)
    output_mode: str = Field("full_systematic", description="full_systematic / findings_only")


class PrepareResponse(BaseModel):
    """Структурированный результат prepare для Hermes."""

    normalized: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    references_used: list[str] = Field(default_factory=list)
    references: list[RagReferenceInfo] = Field(default_factory=list)
    rag_status: str = "unknown"  # used / no_hits / unavailable / error


class SaveDraftRequest(BaseModel):
    """Создать draft case из prepared JSON + assistant_draft."""

    prepared: dict[str, Any] = Field(..., description="JSON from prepare response")
    assistant_draft: str = Field(..., description="Hermes-generated draft text")
    references_used: list[str] = Field(default_factory=list)


class DraftResponse(BaseModel):
    case_id: str
    draft: str
    references_used: list[str] = []
    path: str


class AcceptRequest(BaseModel):
    save_as_reference: bool = False


class ReferenceOutcome(BaseModel):
    """Структурированный результат promotion/reindex для честного отчета."""

    requested: bool = False
    saved: bool = False
    reference_id: str = ""
    path: str = ""
    legacy_path: str = ""
    index_updated: bool = False
    index_error: str = ""
    skip_reason: str = ""


class CaseActionResponse(BaseModel):
    case_id: str
    status: str
    path: str
    saved_as_reference: bool = False
    reference: ReferenceOutcome = Field(default_factory=ReferenceOutcome)


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
    reference: ReferenceOutcome = Field(default_factory=ReferenceOutcome)

class ReferenceLifecycleUpdate(BaseModel):
    reference_status: str | None = Field(None, description="active/gold/deprecated/needs_review/rejected")
    quality: str | None = Field(None, description="gold/high/standard/low")
    style_version: str | None = None

class ReferenceLifecycleInfo(BaseModel):
    reference_id: str
    path: str
    reference_status: str
    quality: str
    style_version: str
    task: str
    area: list[str]
    created_at: str
    updated_at: str
    lifecycle_score: float


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


# Назначение: получить локальный RAG-контекст для Hermes перед генерацией черновика.
# Вход: описание/находки, задача, область и клинический контекст.
# Выход: готовый prompt + список использованных reference-файлов; backend не вызывает LLM.
def _build_rag_context(req: RagContextRequest) -> RagContextResponse:
    if not req.input_text.strip():
        raise HTTPException(400, "input_text не может быть пустым")

    try:
        retriever = get_retriever()
        from .area_normalizer import normalize_area
        area = normalize_area(req.area[0]) if req.area else ""
        references = retriever.search(
            req.input_text,
            area=area,
            task=req.task,
            top_k=req.top_k,
        )
        from .prompt_builder import build_prompt

        prompt = build_prompt(
            req.input_text,
            references,
            mode=req.mode,
            clinical_context=req.clinical_context,
            task=req.task,
            areas=req.area,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"RAG context error: {e}")

    reference_infos = [
        RagReferenceInfo(
            filepath=ref.filepath,
            title=getattr(ref, "title", ""),
            area=ref.area,
            similarity=round(float(ref.similarity), 4),
        )
        for ref in references
    ]
    return RagContextResponse(
        prompt=prompt,
        references_used=[ref.filepath for ref in references],
        references=reference_infos,
    )


# --- Endpoints ---

@app.post("/api/rag/context", response_model=RagContextResponse)
async def build_rag_context(req: RagContextRequest):
    """Возвращает локальный few-shot/RAG prompt для Hermes без вызова LLM."""
    return _build_rag_context(req)


@app.post("/api/prepare", response_model=PrepareResponse)
async def prepare_radi_ct(req: PrepareRequest):
    """Единая operational entry point: parse + RAG retrieval + metadata.

    Возвращает structured JSON: normalized metadata, prompt, references,
    rag_status. Hermes использует prompt для генерации черновика, затем
    вызывает /api/save-draft с prepared JSON и assistant_draft.
    """
    if not req.input_text.strip():
        raise HTTPException(400, "input_text не может быть пустым")

    normalized = {
        "input_text": req.input_text,
        "task": req.task,
        "area": req.area,
        "clinical_context": req.clinical_context,
        "comparison": req.comparison,
        "mode": req.mode,
    }

    # RAG retrieval
    rag_status = "unknown"
    prompt = ""
    references_used: list[str] = []
    references: list[RagReferenceInfo] = []

    try:
        retriever = get_retriever()
        from .area_normalizer import normalize_area
        area = normalize_area(req.area[0]) if req.area else ""
        refs = retriever.search(
            req.input_text,
            area=area,
            task=req.task,
            top_k=req.top_k,
        )
        from .prompt_builder import build_prompt

        prompt = build_prompt(
            req.input_text,
            refs,
            mode=req.mode,
            clinical_context=req.clinical_context,
            task=req.task,
            areas=req.area,
            output_mode=req.output_mode,
        )
        references_used = [ref.filepath for ref in refs]
        references = [
            RagReferenceInfo(
                filepath=ref.filepath,
                title=getattr(ref, "title", ""),
                area=ref.area,
                similarity=round(float(ref.similarity), 4),
            )
            for ref in refs
        ]
        rag_status = "used" if refs else "no_hits"
    except ValueError as e:
        rag_status = "unavailable"
        # RAG unavailable is not a hard error — Hermes can still draft
    except Exception as e:
        rag_status = "error"
        # RAG error is not a hard error — Hermes can still draft

    return PrepareResponse(
        normalized=normalized,
        prompt=prompt,
        references_used=references_used,
        references=references,
        rag_status=rag_status,
    )


@app.post("/api/save-draft", response_model=DraftResponse)
async def save_draft(req: SaveDraftRequest):
    """Создать draft case из prepared JSON + assistant_draft.

    Единая точка для сохранения черновика после prepare.
    references_used сохраняются вместе с case.
    """
    if not req.assistant_draft.strip():
        raise HTTPException(400, "assistant_draft обязателен")

    prepared = req.prepared or {}
    input_text = prepared.get("input_text", "")
    if not input_text:
        # Prepare returns input_text inside normalized dict
        normalized = prepared.get("normalized", {})
        input_text = normalized.get("input_text", "")
    if not input_text:
        raise HTTPException(400, "prepared.input_text is required")

    store = get_feedback_store()
    normalized = prepared.get("normalized", {})
    record = store.create_case(
        input_text=input_text,
        assistant_draft=req.assistant_draft.strip(),
        task=prepared.get("task", normalized.get("task", "conclusion")),
        area=prepared.get("area", normalized.get("area", [])),
        clinical_context=prepared.get("clinical_context", normalized.get("clinical_context", "")),
        comparison=prepared.get("comparison", normalized.get("comparison", False)),
        references_used=req.references_used or prepared.get("references_used", normalized.get("references_used", [])),
    )
    return DraftResponse(
        case_id=record.metadata.case_id,
        draft=record.assistant_draft,
        references_used=record.metadata.references_used,
        path=str(store.drafts_dir / f"{record.metadata.case_id}.md"),
    )


@app.post("/api/draft", response_model=DraftResponse)
async def create_draft(req: DraftRequest):
    """Создаёт learning-loop draft case и возвращает case_id.

    Hermes-only contract: backend не вызывает LLM и не генерирует текст.
    `assistant_draft` обязателен и должен быть сформирован Hermes в текущей
    Telegram/agent-сессии на основе входа и локально найденных references.
    """
    if not req.input_text.strip():
        raise HTTPException(400, "input_text не может быть пустым")

    draft = req.assistant_draft.strip()
    if not draft:
        raise HTTPException(400, "assistant_draft обязателен в Hermes-only режиме")

    store = get_feedback_store()
    record = store.create_case(
        input_text=req.input_text,
        assistant_draft=draft,
        task=req.task,
        input_type=req.input_type,
        area=req.area,
        clinical_context=req.clinical_context,
        comparison=req.comparison,
        references_used=req.references_used,
    )
    return DraftResponse(
        case_id=record.metadata.case_id,
        draft=record.assistant_draft,
        references_used=record.metadata.references_used,
        path=str(store.drafts_dir / f"{record.metadata.case_id}.md"),
    )


def _promotion_outcome(
    store: FeedbackStore,
    promotion_result: Any,
    requested: bool,
) -> ReferenceOutcome:
    """Преобразовать PromotionResult в API ReferenceOutcome."""
    if not requested:
        return ReferenceOutcome(requested=False, skip_reason="save_as_reference=False")
    if promotion_result is None:
        return ReferenceOutcome(requested=True, saved=False, skip_reason="promotion_not_performed")
    return ReferenceOutcome(
        requested=True,
        saved=promotion_result.saved,
        reference_id=promotion_result.reference_id,
        path=promotion_result.path,
        legacy_path=promotion_result.legacy_path,
        index_updated=promotion_result.index_updated,
        index_error=promotion_result.index_error,
        skip_reason=promotion_result.skip_reason,
    )


@app.post("/api/accept/{case_id}", response_model=CaseActionResponse)
async def accept_case(case_id: str, req: AcceptRequest):
    """Принимает draft без правок и пишет feedback event."""
    store = get_feedback_store()
    try:
        record, promotion_result = store.accept_case(
            case_id, save_as_reference=req.save_as_reference
        )
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    return CaseActionResponse(
        case_id=record.metadata.case_id,
        status=record.metadata.status,
        path=str(store.accepted_dir / f"{record.metadata.case_id}.md"),
        saved_as_reference=req.save_as_reference and promotion_result is not None and promotion_result.saved,
        reference=_promotion_outcome(store, promotion_result, req.save_as_reference),
    )


@app.post("/api/correct/{case_id}", response_model=CaseActionResponse)
async def correct_case(case_id: str, req: CorrectRequest):
    """Сохраняет финальную правку Романа, feedback и error tags."""
    if not req.roman_final.strip():
        raise HTTPException(400, "roman_final не может быть пустым")

    store = get_feedback_store()
    try:
        record, promotion_result = store.correct_case(
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
        saved_as_reference=req.save_as_reference and promotion_result is not None and promotion_result.saved,
        reference=_promotion_outcome(store, promotion_result, req.save_as_reference),
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


@app.get("/api/references/lifecycle", response_model=list[ReferenceLifecycleInfo])
async def list_reference_lifecycle(status: str | None = None, include_inactive: bool = True):
    """Показывает reference base с lifecycle metadata для ревизии качества."""
    store = get_feedback_store()
    try:
        return [ReferenceLifecycleInfo(**item) for item in store.list_references(status=status, include_inactive=include_inactive)]
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/references/lifecycle/{reference_id}", response_model=ReferenceLifecycleInfo)
async def update_reference_lifecycle(reference_id: str, req: ReferenceLifecycleUpdate):
    """Помечает reference как active/gold/deprecated/needs_review/rejected и обновляет индекс."""
    store = get_feedback_store()
    try:
        store.update_reference_lifecycle(
            reference_id,
            reference_status=req.reference_status,
            quality=req.quality,
            style_version=req.style_version,
        )
    except FileNotFoundError:
        raise HTTPException(404, f"Reference not found: {reference_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    matches = [item for item in store.list_references() if item["reference_id"] == reference_id]
    if not matches:
        raise HTTPException(404, f"Reference not found after update: {reference_id}")
    return ReferenceLifecycleInfo(**matches[0])


@app.post("/api/references/promote/{case_id}", response_model=PromoteResponse)
async def promote_case_to_reference(case_id: str):
    """Явно переносит accepted/corrected case в few-shot reference base.

    Promotion может упасть с 400, если case ещё draft, нет финального текста
    или базовый PHI guard нашёл прямой идентификатор.
    """
    store = get_feedback_store()
    try:
        promotion_result = store.promote_to_reference(case_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Case not found: {case_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return PromoteResponse(
        case_id=case_id,
        reference_path=promotion_result.path,
        reference=ReferenceOutcome(
            requested=True,
            saved=promotion_result.saved,
            reference_id=promotion_result.reference_id,
            path=promotion_result.path,
            legacy_path=promotion_result.legacy_path,
            index_updated=promotion_result.index_updated,
            index_error=promotion_result.index_error,
            skip_reason=promotion_result.skip_reason,
        ),
    )


@app.post("/api/reindex", response_model=ReindexResponse)
async def reindex(force: bool = False):
    """Перестраивает активный RAG index: OHS reference vault или legacy Chroma."""
    from .rag import reindex_active_backend

    try:
        result = reindex_active_backend(force=force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"RAG reindex error: {e}")
    return ReindexResponse(**result)


@app.get("/api/rag/status", response_model=RagStatusResponse)
async def get_rag_status():
    """Показывает готовность активного RAG backend-а."""
    from .rag import rag_status

    return RagStatusResponse(**rag_status())


@app.get("/api/references")
async def list_references():
    """Список всех референсов в активной Markdown-базе."""
    reference_dir = REFERENCE_VAULT_DIR if RAG_BACKEND in {"obsidian_hybrid", "ohs", "obsidian"} else REFERENCES_DIR
    entries = parse_directory(reference_dir)
    return [
        ReferenceInfo(
            filepath=e.filepath,
            area=e.area,
            doctor=e.doctor,
            date=str(e.metadata.get("дата", "")),
        )
        for e in entries
    ]


# --- Session state models ---

class SessionStateRequest(BaseModel):
    session_id: str = Field(..., description="Hermes session identifier")
    case_id: str = Field("", description="Active RadiCT case_id")
    state: str = Field("awaiting_feedback", description="Workflow state")
    task: str = Field("conclusion", description="conclusion / description / description_and_conclusion")
    rag_status: str = Field("unknown", description="used / no_hits / unavailable / error / unknown")


class SessionStateResponse(BaseModel):
    session_id: str
    case_id: str = ""
    state: str = ""
    task: str = ""
    rag_status: str = ""
    updated_at: str = ""


@app.post("/api/session/state", response_model=SessionStateResponse)
async def set_session_state(req: SessionStateRequest):
    """Связать Hermes session_id с активным RadiCT case_id."""
    store = get_session_state_store()
    entry = store.set_active_case(
        session_id=req.session_id,
        case_id=req.case_id,
        state=req.state,
        task=req.task,
        rag_status=req.rag_status,
    )
    return SessionStateResponse(
        session_id=req.session_id,
        case_id=entry.get("case_id", ""),
        state=entry.get("state", ""),
        task=entry.get("task", ""),
        rag_status=entry.get("rag_status", ""),
        updated_at=entry.get("updated_at", ""),
    )


@app.get("/api/session/state/{session_id}", response_model=SessionStateResponse)
async def get_session_state(session_id: str):
    """Получить активный case_id для session_id."""
    store = get_session_state_store()
    entry = store.get_active_case(session_id)
    if entry is None:
        raise HTTPException(404, f"No active case for session: {session_id}")
    return SessionStateResponse(
        session_id=session_id,
        case_id=entry.get("case_id", ""),
        state=entry.get("state", ""),
        task=entry.get("task", ""),
        rag_status=entry.get("rag_status", ""),
        updated_at=entry.get("updated_at", ""),
    )


@app.delete("/api/session/state/{session_id}")
async def clear_session_state(session_id: str):
    """Очистить активный case для session_id."""
    store = get_session_state_store()
    store.clear(session_id)
    return {"status": "ok", "session_id": session_id}


@app.get("/api/session/states")
async def list_session_states():
    """Показать все активные session→case mappings."""
    store = get_session_state_store()
    return store.list_active()


@app.get("/api/health")
async def health():
    """Проверка работоспособности."""
    return {"status": "ok", "version": "0.1.0"}
