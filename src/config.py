"""
Конфигурация RadiCT Assistant.
Настройки читаются из .env или переменных окружения.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- LLM ---
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2000"))
LLM_TIMEOUT = 60
# По умолчанию backend НЕ вызывает внешнюю LLM: Hermes генерирует черновик
# в чате и передает assistant_draft в /api/draft. Включать только для
# обезличенных данных и осознанного OpenAI-compatible API контура.
ENABLE_EXTERNAL_LLM = os.getenv("RADI_CT_ENABLE_EXTERNAL_LLM", "0").strip().lower() in {"1", "true", "yes", "да"}

# --- Embedding ---
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# --- Paths ---
BASE_DIR = Path(__file__).parent.parent
REFERENCES_DIR = BASE_DIR / os.getenv("REFERENCES_DIR", "data/references")
# Отдельный Obsidian-like vault для RAG examples. Это НЕ personal-base и не
# working-base-syncthing: сюда попадают только accepted/corrected обезличенные
# cases, прошедшие PHI guard.
REFERENCE_VAULT_DIR = BASE_DIR / os.getenv("RADI_CT_REFERENCE_VAULT_DIR", "data/reference-vault")
INDEX_DIR = BASE_DIR / os.getenv("INDEX_DIR", "data/index")
PROMPTS_DIR = BASE_DIR / "prompts"

# --- RAG backend ---
RAG_BACKEND = os.getenv("RAG_BACKEND", "obsidian_hybrid")
OHS_COMMAND = os.getenv("OHS_COMMAND", "obsidian-hybrid-search")
OHS_TIMEOUT = int(os.getenv("OHS_TIMEOUT", "120"))
# После promotion accepted/corrected case в reference vault можно сразу обновлять
# OHS-индекс, чтобы следующий запрос уже видел новый пример.
AUTO_REINDEX_REFERENCES = os.getenv("RADI_CT_AUTO_REINDEX", "1").strip().lower() in {"1", "true", "yes", "да"}

# --- Retrieval ---
TOP_K = int(os.getenv("TOP_K", "5"))
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.5"))

# --- Server ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8420"))