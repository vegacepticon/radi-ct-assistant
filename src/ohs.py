"""
Интеграция RadiCT Assistant с Obsidian Hybrid Search (OHS).

Зачем нужен этот модуль:
- OHS умеет искать по Markdown/Obsidian vault гибридно: BM25 + semantic vector search.
- RadiCT Assistant хранит reference examples как .md файлы, поэтому OHS подходит
  как RAG-слой лучше, чем отдельный ChromaDB-прототип.
- Модуль не импортирует Node/JS библиотеки напрямую. Он вызывает CLI
  `obsidian-hybrid-search` через subprocess, поэтому Python-тесты могут работать
  даже если OHS не установлен.

Безопасность:
- По умолчанию индексируется только локальный vault проекта:
  data/reference-vault или путь из RADI_CT_REFERENCE_VAULT_DIR.
- personal-base и working-base-syncthing здесь не используются.
- Перед запуском CLI из окружения убираются OPENAI_* переменные, чтобы OHS
  использовал локальные embeddings, а не внешний API.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import OHS_COMMAND, OHS_TIMEOUT, REFERENCE_VAULT_DIR, TOP_K, MIN_SIMILARITY
from .feedback_store import reference_lifecycle_score
from .parser import parse_file
from .area_normalizer import any_area_match, normalize_area


@dataclass(slots=True)
class OhsStatus:
    """Короткий статус OHS backend-а для API health/readiness."""

    backend: str
    available: bool
    command: str
    vault: str
    total: int = 0
    indexed: int = 0
    chunks: int = 0
    model: str = ""
    version: str = ""
    error: str = ""


@dataclass(slots=True)
class OhsRetrievalResult:
    """Результат поиска, совместимый с prompt_builder.build_prompt()."""

    description: str
    conclusion: str
    recommendation: str
    similarity: float
    area: str
    doctor: str
    filepath: str
    title: str = ""


# Назначение: найти исполняемый файл obsidian-hybrid-search.
# Вход: строка из конфига OHS_COMMAND; может быть абсолютным путем или именем.
# Выход: абсолютный путь/имя команды, пригодный для subprocess, или None.
def resolve_ohs_command(command: str = OHS_COMMAND) -> str | None:
    if not command:
        return None

    candidate = Path(command).expanduser()
    if candidate.is_absolute() or "/" in command:
        return str(candidate) if candidate.exists() and os.access(candidate, os.X_OK) else None

    found = shutil.which(command)
    if found:
        return found

    # На этом Raspberry Pi OHS установлен в Hermes-managed Node prefix, который
    # не всегда попадает в PATH systemd-сервиса radi-ct-assistant.
    hermes_node_candidate = Path.home() / ".hermes" / "node" / "bin" / command
    if hermes_node_candidate.exists() and os.access(hermes_node_candidate, os.X_OK):
        return str(hermes_node_candidate)

    return None


# Назначение: собрать безопасное окружение для OHS CLI.
# Вход: vault path.
# Выход: env dict для subprocess.run().
def _ohs_env(vault_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OBSIDIAN_VAULT_PATH"] = str(vault_dir)

    # Принудительно не даём OHS случайно уйти во внешний OpenAI-compatible API.
    for key in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_EMBEDDING_MODEL"]:
        env.pop(key, None)

    hermes_node_bin = str(Path.home() / ".hermes" / "node" / "bin")
    env["PATH"] = f"{hermes_node_bin}:{env.get('PATH', '')}"
    return env


# Назначение: выполнить OHS CLI и вернуть stdout.
# Вход: аргументы CLI без самой команды, vault_dir и timeout.
# Выход: stdout; при ошибке бросает RuntimeError с stderr/stdout.
def run_ohs(args: list[str], vault_dir: Path = REFERENCE_VAULT_DIR, timeout: int = OHS_TIMEOUT) -> str:
    command = resolve_ohs_command()
    if not command:
        raise RuntimeError(
            "obsidian-hybrid-search command not found. "
            "Install it or set OHS_COMMAND=/absolute/path/to/obsidian-hybrid-search"
        )

    vault_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [command, *args],
        cwd=str(vault_dir),
        env=_ohs_env(vault_dir),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "OHS command failed").strip()
        raise RuntimeError(message)
    return completed.stdout.strip()


# Назначение: получить JSON status от OHS и нормализовать его для API.
# Вход: vault_dir.
# Выход: OhsStatus с available=true/false и диагностикой.
def ohs_status(vault_dir: Path = REFERENCE_VAULT_DIR) -> OhsStatus:
    command = resolve_ohs_command()
    if not command:
        return OhsStatus(
            backend="obsidian_hybrid",
            available=False,
            command=OHS_COMMAND,
            vault=str(vault_dir),
            error="obsidian-hybrid-search command not found",
        )

    try:
        output = run_ohs(["status"], vault_dir=vault_dir)
        data = json.loads(output)
        return OhsStatus(
            backend="obsidian_hybrid",
            available=True,
            command=command,
            vault=str(vault_dir),
            total=int(data.get("total") or 0),
            indexed=int(data.get("indexed") or 0),
            chunks=int(data.get("chunks") or 0),
            model=str(data.get("model") or ""),
            version=str(data.get("version") or ""),
        )
    except Exception as e:
        return OhsStatus(
            backend="obsidian_hybrid",
            available=False,
            command=command,
            vault=str(vault_dir),
            error=str(e),
        )


# Назначение: переиндексировать reference vault через OHS.
# Вход: force=True для полной пересборки, False для incremental.
# Выход: status dict после reindex.
def ohs_reindex(vault_dir: Path = REFERENCE_VAULT_DIR, force: bool = False) -> OhsStatus:
    args = ["reindex"]
    if force:
        args.append("--force")
    run_ohs(args, vault_dir=vault_dir, timeout=max(OHS_TIMEOUT, 300))
    return ohs_status(vault_dir=vault_dir)


class ObsidianHybridRetriever:
    """RAG retriever на базе obsidian-hybrid-search."""

    # Назначение: создать retriever для отдельного локального reference vault.
    # Вход: vault_dir; по умолчанию data/reference-vault внутри проекта.
    # Выход: объект, умеющий искать похожие cases.
    def __init__(self, vault_dir: Path = REFERENCE_VAULT_DIR):
        self.vault_dir = vault_dir

    # Назначение: найти похожие reference examples.
    # Вход:
    #   query_description — текущее описание/черновые находки;
    #   area — область исследования, например "ОГК";
    #   task — conclusion / description / description_and_conclusion;
    #   top_k — сколько examples вернуть.
    # Выход: список OhsRetrievalResult, совместимый с build_prompt().
    #
    # Phase 6 improvements:
    # - Dedup by filepath (OHS may return same file from different chunks)
    # - Relative confidence: score gap top-1/top-2
    # - Diversity: prefer non-identical examples (simple MMR-like)
    # - no_good_hits: if best candidate below absolute threshold, return empty
    def search(
        self,
        query_description: str,
        area: str = "",
        task: str = "conclusion",
        top_k: int = TOP_K,
        min_similarity: float = MIN_SIMILARITY,
    ) -> list[OhsRetrievalResult]:
        base_args = [
            "search",
            "--json",
            "--mode",
            "hybrid",
            "--limit",
            str(max(top_k * 3, top_k)),
            "--frontmatter",
            "статус:true",
        ]

        # OHS применяет frontmatter-фильтры до своего --limit. Это критично для
        # редких task-пулов: без task-фильтра немногочисленные finding_description
        # references могут не попасть в первые N результатов среди заключений.
        # Выполняем два совместимых запроса: v2 хранит `task`, legacy — `задача`.
        task_aliases = {
            "conclusion": "заключение",
            "description": "описание",
            "finding_description": "описание_находки",
            "description_and_conclusion": "описание_и_заключение",
        }
        # Дополнительные schema-aware запросы нужны только новому редкому task.
        # Для существующих задач сохраняем один широкий запрос: старые references
        # могут вообще не иметь task-поля, и серверный фильтр исключил бы их.
        task_filters = [""]
        if task == "finding_description":
            task_filters = [
                f"task:{task}",
                f"задача:{task_aliases[task]}",
            ]

        raw_results: list[dict[str, Any]] = []
        for task_filter in task_filters:
            args = list(base_args)
            if task_filter:
                args.extend(["--frontmatter", task_filter])
            # Область по-прежнему фильтруется локально: OHS-поддержка массивов
            # зависит от версии, а `areas`/`область` также различаются по schema.
            args.append(query_description)
            output = run_ohs(args, vault_dir=self.vault_dir)
            parsed_output = json.loads(output or "[]")
            if isinstance(parsed_output, list):
                raw_results.extend(parsed_output)
        raw_results.sort(key=lambda item: float(item.get("score") or 0), reverse=True)

        # --- Dedup by filepath ---
        # OHS может вернуть один файл несколько раз (разные chunks).
        # Берём только первую (лучшую) запись для каждого файла.
        seen_paths: set[str] = set()
        deduped_results: list[dict[str, Any]] = []
        for raw in raw_results:
            path = raw.get("path")
            if not path:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            deduped_results.append(raw)

        candidates: list[OhsRetrievalResult] = []
        for raw in deduped_results:
            score = float(raw.get("score") or 0)
            if score < min_similarity:
                continue

            relative_path = raw.get("path")
            if not relative_path:
                continue

            filepath = self.vault_dir / str(relative_path)
            entry = parse_file(filepath)
            if not entry or not entry.is_quality:
                continue

            # OHS filter — только prefilter: schema v1/v2 используют поля
            # «задача» и «task». Локальная повторная проверка не позволяет
            # смешивать task-пулы даже при различиях версий OHS.
            ref_task = str(
                entry.metadata.get("task")
                or entry.metadata.get("задача")
                or "conclusion"
            )
            if task and ref_task != task:
                continue

            # Multi-area matching: reference с [ОГК, ОБП, ОМТ] находится
            # по запросу любой из трех областей.
            ref_areas = entry.metadata.get("areas") or entry.metadata.get("область", [])
            if not isinstance(ref_areas, list):
                ref_areas = [ref_areas] if ref_areas else []
            if area and not any_area_match([area], ref_areas):
                continue

            lifecycle_score = reference_lifecycle_score(entry.metadata)
            candidates.append(
                OhsRetrievalResult(
                    description=entry.description,
                    conclusion=entry.conclusion,
                    recommendation=entry.recommendation,
                    similarity=(score * 0.8) + (lifecycle_score * 0.2),
                    area=entry.area,
                    doctor=entry.doctor,
                    filepath=str(filepath),
                    title=str(raw.get("title") or filepath.stem),
                )
            )

        # --- no_good_hits: absolute threshold ---
        # Если лучший candidate ниже ABSOLUTE_MIN_SIMILARITY,
        # возвращаем пустой список — лучше не дать examples, чем дать плохие.
        ABSOLUTE_MIN_SIMILARITY = 0.45
        if not candidates or candidates[0].similarity < ABSOLUTE_MIN_SIMILARITY:
            return []

        candidates.sort(key=lambda item: item.similarity, reverse=True)

        # --- Diversity: simple MMR-like ---
        # Если среди top результатов есть файлы с очень похожим description,
        # оставляем только один из них. Простейшая эвристика: если два файла
        # имеют description, совпадающий по первым 200 символам > 80%, 
        # оставляем только тот, у кого выше score.
        diverse: list[OhsRetrievalResult] = []
        for cand in candidates:
            is_duplicate = False
            for kept in diverse:
                # Сравниваем первые 200 символов description
                desc_a = cand.description[:200].strip().lower()
                desc_b = kept.description[:200].strip().lower()
                if desc_a and desc_b:
                    # Простой overlap: доля общих слов
                    words_a = set(desc_a.split())
                    words_b = set(desc_b.split())
                    if words_a and words_b:
                        overlap = len(words_a & words_b) / len(words_a | words_b)
                        if overlap > 0.85:
                            is_duplicate = True
                            break
            if not is_duplicate:
                diverse.append(cand)
            if len(diverse) >= top_k:
                break

        return diverse[:top_k]
