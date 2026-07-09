# Архитектура RadiCT Assistant

## Обзор

`radi-ct-assistant` — локальный store/RAG/lifecycle сервис для работы с CT reference base. Он **не генерирует медицинский текст через внешние LLM API**. Генерацию черновика выполняет Hermes Agent в текущей Telegram/agent-сессии, используя локально найденные references как контекст. Backend сохраняет `assistant_draft`, feedback, финальные правки и reference lifecycle.

```text
Telegram / Hermes
  → входное описание + метаданные
  → локальный retrieval по data/reference-vault/
  → Hermes формирует черновик
  → POST /api/draft с обязательным assistant_draft
  → accept/correct
  → PHI guard
  → reference lifecycle + OHS reindex
```

## Компоненты

### 1. Reference vault

Файлы хранятся как Markdown с YAML frontmatter в `data/reference-vault/`. В retrieval участвуют только обезличенные, проверенные examples:

```yaml
---
анамнез: null
область:
  - ОГК
сравнение: true
экстренность: false
статус: true
reference_status: active
quality: standard
style_version: 2026-07
created_at: 2026-07
updated_at: 2026-07
---

Описание...

Заключение:
Финальный вариант...
```

`deprecated`, `needs_review` и `rejected` исключаются из retrieval. `gold` и более качественные/новые examples получают приоритет.

### 2. Retrieval

Основной backend — Obsidian Hybrid Search (`obsidian-hybrid-search`) по отдельному reference vault. OHS запускается локально с `local:Xenova/multilingual-e5-small`; переменные `OPENAI_*` принудительно удаляются из окружения OHS-процесса, чтобы не уйти во внешний embedding API.

Старый Chroma backend остаётся только как legacy retrieval fallback через `RAG_BACKEND=chroma`; он не связан с генерацией.

### 3. Hermes-only generation

Backend не содержит `/api/generate` и не содержит `llm_client.py`.

Генерация происходит так:

1. Hermes получает запрос Романа.
2. Hermes/инструменты получают локальные references/RAG context.
3. Hermes формирует черновик описания/заключения.
4. Hermes сохраняет case через `/api/draft`, передавая `assistant_draft`.

Если `assistant_draft` отсутствует, `/api/draft` возвращает ошибку. Это архитектурный guardrail.

## FastAPI endpoints

| Endpoint | Назначение |
|---|---|
| `GET /api/health` | Проверка сервиса |
| `POST /api/draft` | Сохранить Hermes-generated draft case; `assistant_draft` обязателен |
| `POST /api/accept/{case_id}` | Принять draft без правок |
| `POST /api/correct/{case_id}` | Сохранить финальный вариант Романа и feedback |
| `GET /api/cases` | Список cases |
| `GET /api/cases/{case_id}` | Детали case |
| `POST /api/references/promote/{case_id}` | Перенести accepted/corrected case в reference base после PHI guard |
| `GET /api/references/lifecycle` | Список references с lifecycle metadata |
| `PATCH /api/references/lifecycle/{reference_id}` | Изменить `reference_status`, `quality`, `style_version` |
| `POST /api/reindex` | Обновить индекс references |
| `GET /api/rag/status` | Статус RAG backend |

## Безопасность

- `working-base-syncthing` и реальные patient data не используются как source.
- В reference vault должны попадать только обезличенные examples.
- Promotion выполняет PHI guard.
- Backend не отправляет медицинский текст во внешние LLM API.
- Exact dates в lifecycle metadata не используются; применяется `YYYY-MM`.

## Развёртывание

Сервис работает как user-level systemd unit на Raspberry Pi:

```bash
systemctl --user status radi-ct-assistant.service
python3 scripts/radi_ct_workflow.py health
```

Основной локальный путь: `/home/hermes/projects/radi-ct-assistant`.
