"""
LLM API клиент — вызов DeepSeek / Claude / Gemini через OpenAI-compatible API.
"""
import httpx
from .config import (
    LLM_API_BASE,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT,
    ENABLE_EXTERNAL_LLM,
)


async def generate(prompt: str) -> str:
    """
    Отправляет промпт во внешнюю LLM API и возвращает сгенерированный текст.

    По умолчанию этот путь выключен: рабочий контур Романа должен идти через
    Hermes, где черновик формируется в Telegram-сессии и передается в backend
    как assistant_draft. Это защищает от случайной отправки клинического текста
    во внешний API.
    """
    if not ENABLE_EXTERNAL_LLM:
        raise RuntimeError(
            "External LLM generation is disabled. Use Hermes-generated "
            "assistant_draft or set RADI_CT_ENABLE_EXTERNAL_LLM=1 for "
            "explicit anonymized API generation."
        )
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        response = await client.post(
            f"{LLM_API_BASE}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


def generate_sync(prompt: str) -> str:
    """Синхронная версия для скриптов."""
    if not ENABLE_EXTERNAL_LLM:
        raise RuntimeError(
            "External LLM generation is disabled. Use Hermes-generated "
            "assistant_draft or set RADI_CT_ENABLE_EXTERNAL_LLM=1."
        )
    import requests

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }

    response = requests.post(
        f"{LLM_API_BASE}/chat/completions",
        headers=headers,
        json=payload,
        timeout=LLM_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]