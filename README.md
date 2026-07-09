# RadiCT Assistant

AI-ассистент для формирования заключений КТ на основе описательной части исследования.

## Проблема

Радиолог пишет описательную часть вручную, затем хочет получить готовое заключение в своём стиле — правильной структуры, формулировок, уровня детализации. Текущий подход (статичный промпт + LLM) даёт неудовлетворительный результат: не та последовательность, неправильная группировка, неподходящие формулировки.

## Решение

Few-shot retrieval: динамически подтягивать релевантные пары «описание → заключение» из базы радиолога и подставлять их в промпт как примеры. Модель видит реальный стиль врача для данного типа исследования, а не абстрактные правила.

### Ключевые компоненты

- **Reference vault** — локальная Obsidian-like Markdown-база `data/reference-vault/` с парами «описание → заключение».
- **Obsidian Hybrid Search RAG** — гибридный поиск `obsidian-hybrid-search`: BM25 + semantic search по reference vault.
- **Learning loop** — accepted/corrected cases автоматически превращаются в reference examples после PHI guard.
- **Retrieval-сервис** — FastAPI на Raspberry Pi, собирает промпт с few-shot примерами.
- **Hermes-only draft workflow** — по умолчанию backend не вызывает внешнюю LLM: Hermes формирует черновик в Telegram-сессии и сохраняет его через `/api/draft` как `assistant_draft`.
- **External LLM fallback** — OpenAI-compatible API выключен по умолчанию и доступен только при явном `RADI_CT_ENABLE_EXTERNAL_LLM=1` для осознанных обезличенных тестов.
- **Reference lifecycle** — старые/сомнительные примеры можно помечать `deprecated`, `needs_review`, `rejected`; retrieval использует только `active`/`gold` и учитывает качество/новизну.

Старый ChromaDB/sentence-transformers backend сохранён как legacy fallback через `RAG_BACKEND=chroma`, но основной backend по умолчанию — `RAG_BACKEND=obsidian_hybrid`.

### Переменные окружения

| Переменная | Значение по умолчанию | Назначение |
|---|---|---|
| `RAG_BACKEND` | `obsidian_hybrid` | Выбор RAG backend: `obsidian_hybrid` или `chroma` |
| `OHS_COMMAND` | `obsidian-hybrid-search` | Команда/путь к OHS CLI |
| `RADI_CT_REFERENCE_VAULT_DIR` | `data/reference-vault` | Путь к локальному reference vault |
| `RADI_CT_AUTO_REINDEX` | `1` | Автоматический OHS reindex после promotion |
| `RADI_CT_BASE_DIR` | корень проекта | Базовая директория для data/ |
| `RADI_CT_ENABLE_EXTERNAL_LLM` | `0` | Явное включение legacy `/api/generate` через внешний OpenAI-compatible API; по умолчанию выключено |

### Архитектура

```‌
[Telegram / Hermes / RadiProtocol]
         ↓ описание + метаданные
   [API сервис на RPi (FastAPI)]
         ↓
   1. Парсинг входных данных
   2. Obsidian Hybrid Search по data/reference-vault/
   3. Фильтр по области/статусу/task
   4. Сборка промпта: системный + 3-5 few-shot + входное описание
         ↓
   [Hermes в Telegram-сессии]
         ↓
   Черновик заключения/описания
         ↓
   /api/draft сохраняет assistant_draft без внешнего LLM API
         ↓
   accept/correct от Романа
         ↓
   PHI guard → новый reference example → OHS reindex
```

## Статус

🚧 Прототип / архитектурные наброски. В активной разработке.

## Документация

- [Learning loop](docs/LEARNING_LOOP.md)
- [Telegram ↔ Hermes workflow](docs/TELEGRAM_HERMES_WORKFLOW.md)

## План

1. **Скрипт очистки .md файлов** от конфиденциальных данных (PHI)
2. **Индексация базы** — embedding описательных частей
3. **FastAPI сервис** — endpoint для генерации заключений
4. **Тестирование через Telegram** → итерация промпта
5. **Интеграция в RadiProtocol** — как встроенная функция

## Стек

- Python 3.11+, FastAPI, uvicorn
- FastAPI + uvicorn на Raspberry Pi 5
- Obsidian Hybrid Search как основной RAG backend
- Hermes Telegram-сессия как основной генератор черновиков
- Legacy OpenAI-compatible API только при явном `RADI_CT_ENABLE_EXTERNAL_LLM=1`

## Лицензия

MIT