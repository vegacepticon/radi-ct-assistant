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

# --- Embedding ---
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# --- Paths ---
BASE_DIR = Path(__file__).parent.parent
REFERENCES_DIR = BASE_DIR / os.getenv("REFERENCES_DIR", "data/references")
INDEX_DIR = BASE_DIR / os.getenv("INDEX_DIR", "data/index")
PROMPTS_DIR = BASE_DIR / "prompts"

# --- Retrieval ---
TOP_K = int(os.getenv("TOP_K", "5"))
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.5"))

# --- Server ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8420"))