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
    def search(
        self,
        query_description: str,
        area: str = "",
        task: str = "conclusion",
        top_k: int = TOP_K,
        min_similarity: float = MIN_SIMILARITY,
    ) -> list[OhsRetrievalResult]:
        args = [
            "search",
            "--json",
            "--mode",
            "hybrid",
            "--limit",
            str(max(top_k * 3, top_k)),
            "--frontmatter",
            "статус:true",
        ]
        if task:
            args.extend(["--frontmatter", f"задача:{task}"])
        if area:
            # OHS frontmatter-фильтр по массивам может зависеть от версии, поэтому
            # область дополнительно фильтруется после парсинга файла ниже.
            pass
        args.append(query_description)

        output = run_ohs(args, vault_dir=self.vault_dir)
        raw_results: list[dict[str, Any]] = json.loads(output or "[]")

        candidates: list[OhsRetrievalResult] = []
        for raw in raw_results:
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
            if area and entry.area != area:
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
        candidates.sort(key=lambda item: item.similarity, reverse=True)
        return candidates[:top_k]
