# Telegram ↔ Hermes workflow для RadiCT Assistant

Документ описывает рекомендуемый Telegram-first workflow: Роман пишет команды в Telegram, Hermes распознаёт сценарий, вызывает локальный `radi-ct-assistant` через `scripts/radi_ct_workflow.py`, а затем возвращает результат в Telegram.

Это **не отдельный Telegram bot** внутри проекта. Telegram остаётся пользовательским интерфейсом, Hermes — оркестратор, `radi-ct-assistant` — локальный backend и хранилище learning-loop cases.

---

## Цели

1. Быстро тестировать RadiCT Assistant из Telegram без отдельного UI.
2. Сохранять draft/correct/feedback/promote события в локальный learning loop.
3. Не смешивать Telegram API, Hermes internals и FastAPI backend в одном слое.
4. Делать сохранение few-shot примеров только по явному разрешению пользователя.
5. Минимизировать риск случайной отправки PHI во внешние LLM/API.

---

## Архитектура

```text
Telegram message
  ↓
Hermes Agent
  ↓
1. Распознать RadiCT-команду
2. Проверить PHI-risk и полноту команды
3. При необходимости подготовить черновик ассистента
4. Вызвать scripts/radi_ct_workflow.py
5. Вернуть Markdown-ответ в Telegram
  ↓
FastAPI RadiCT Assistant
  ↓
data/cases + data/feedback + optional data/references
```

RadiCT Assistant не должен напрямую зависеть от Telegram SDK. Взаимодействие идёт через CLI-контракт.

---

## Основной CLI-контракт

Перед использованием должен быть запущен API:

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Проверка:

```bash
python3 scripts/radi_ct_workflow.py health
```

Создание draft из Telegram/Hermes-сообщения:

```bash
python3 scripts/radi_ct_workflow.py message message.md
```

Raw JSON для программной обработки:

```bash
python3 scripts/radi_ct_workflow.py --json message message.md
```

Принять draft:

```bash
python3 scripts/radi_ct_workflow.py accept CASE_ID
```

Сохранить исправление:

```bash
python3 scripts/radi_ct_workflow.py correct CASE_ID \
  --final final.md \
  --feedback feedback.md \
  --tag incomplete_stable_findings_list \
  --create-lesson-candidate
```

Явно перенести case в reference base:

```bash
python3 scripts/radi_ct_workflow.py promote CASE_ID
```

---

## Поддерживаемые Telegram-инициаторы

| Telegram-сообщение начинается с | Internal task | Назначение |
|---|---|---|
| `РКТ заключение` | `conclusion` | Сформировать заключение по описанию |
| `РКТ описание` | `description` | Сформировать описание из черновых находок |
| `РКТ описание + заключение` | `description_and_conclusion` | Сформировать оба блока |

Порядок важен: `РКТ описание + заключение` должен распознаваться раньше, чем короткий `РКТ описание`.

Hermes также должен поддерживать свободные естественные формулировки, например:

```text
Нужна помощь с заключением. ОГК/ОБП/ОМТ, рак молочной железы, есть динамика. Описание ниже...
```

```text
Помоги оформить описание по черновым находкам...
```

```text
Сделай описание и заключение, область ОГК, сравнение есть...
```

В таких случаях Hermes сам формализует сообщение в canonical wrapper format: определяет task, area, context, comparison и body. Если область, задача или наличие сравнения неочевидны и это влияет на результат, Hermes задаёт короткий уточняющий вопрос. Пользователь не обязан писать служебные поля `РКТ заключение`, `Область`, `---` вручную.

---

## Формат команды draft

Минимальный формат:

```text
РКТ заключение
Область: ОГК
---
Описание: синтетическое описание без PHI.
```

Расширенный формат:

```text
РКТ заключение
Область: ОГК
Контекст: синтетический обезличенный пример
Сравнение: да
Режим: fast
---
Описание: синтетический очаг S8 правого легкого уменьшился. Плевра свободна.

Черновик ассистента:
Уменьшение очага S8 правого легкого.
```

Поддерживаемые metadata-поля:

| Поле | Пример | Значение |
|---|---|---|
| `Область` | `ОГК, ОБП` | список областей через запятую/точку с запятой |
| `Контекст` | `динамическое наблюдение` | обезличенный клинический контекст |
| `Сравнение` | `да` / `нет` | наличие динамического сравнения |
| `Режим` | `fast` / `analytical` | режим генерации, если нет готового draft |
| `Тип ввода` | `markdown` / `text` / `voice_transcript` | источник/тип входного текста |

Если указан блок `Черновик ассистента:`, backend сохраняет case без вызова LLM/retrieval. Это удобно, когда Hermes уже подготовил черновик и нужно только записать learning-loop событие.

---

## Формат correction-сценария в Telegram

Рекомендуемый пользовательский формат:

```text
Исправляю case 2026-07-06-001

Финальный вариант:
Уменьшение очага S8 правого легкого. Плеврального выпота нет.

Почему:
- Указывать релевантные отрицательные стабильные находки.
- Не заменять список стабильных находок общей фразой.

Теги:
- incomplete_stable_findings_list
```

Hermes должен преобразовать это в:

```bash
python3 scripts/radi_ct_workflow.py correct 2026-07-06-001 \
  --final /tmp/radi-ct-final.md \
  --feedback /tmp/radi-ct-feedback.md \
  --tag incomplete_stable_findings_list \
  --create-lesson-candidate
```

Правило: если `case_id` отсутствует или неоднозначен, Hermes должен спросить уточнение, а не угадывать.

---

## Формат accept-сценария

Пользователь может написать:

```text
Принять case 2026-07-06-001
```

или:

```text
Ок, принять 2026-07-06-001
```

Hermes вызывает:

```bash
python3 scripts/radi_ct_workflow.py accept 2026-07-06-001
```

Если пользователь добавляет `сохрани как пример`, это не должно молча превращаться в `accept --save-as-reference`, пока PHI-риск не проверен и intent не очевиден. Безопаснее сначала принять case, затем отдельно выполнить `promote`.

---

## Формат promote-сценария

Few-shot reference base пополняется только по явному разрешению.

Допустимые фразы:

```text
Сохрани case 2026-07-06-001 как пример
```

```text
Promote case 2026-07-06-001
```

Hermes вызывает:

```bash
python3 scripts/radi_ct_workflow.py promote 2026-07-06-001
```

Backend выполняет базовый PHI guard и вернёт ошибку, если case похож на содержащий PHI.

---

## PHI-safety policy для Telegram/Hermes

Telegram workflow удобен, но небезопасен как место для реальных идентификаторов. Поэтому Hermes должен использовать conservative policy.

### Нельзя автоматически отправлять во внешнюю LLM/API

Остановиться и попросить обезличить текст, если есть:

- ФИО пациента;
- дата рождения пациента;
- длинный ID / номер исследования / номер карты / номер полиса;
- телефон;
- название учреждения, кабинета, врача;
- YAML-ключи вроде `id`, `пациент`, `фио`, `учреждение`, `врач`, `дата рождения`;
- любой явный персональный идентификатор.

### Клинические даты не являются hard-stop blocker

Точные даты исследований, операций, госпитализаций или контрольных сравнений могут быть клинически важны для динамики. В этом workflow они **не считаются самостоятельным PHI-blocker**, если рядом нет прямых идентификаторов пациента.

Примеры допустимого клинического контекста:

```text
В сравнении с исследованием от 06.02.2026...
```

```text
Мастэктомия слева в 2014 г.
```

Если пользователь просит сохранить case в long-term reference base, предпочтительно по возможности нормализовать даты (`предыдущее исследование`, `отдаленный анамнез`), но Hermes не должен ломать рабочий процесс только из-за точной даты исследования/операции.

### Разрешено для локального learning loop

- synthetic examples;
- уже обезличенные описания;
- тексты без персональных идентификаторов;
- клинические даты без других идентификаторов;
- metadata только из safe-полей: `Область`, `Контекст`, `Сравнение`, `Режим`, `Тип ввода`.

### Сохранение reference example

Даже если draft/correct выполнен, `promote` выполняется только после явного запроса пользователя.

---

## Recommended Hermes behavior

Когда Hermes получает Telegram-сообщение:

1. Если сообщение начинается с `РКТ заключение`, `РКТ описание` или `РКТ описание + заключение`:
   - проверить PHI-risk;
   - если безопасно, записать сообщение во временный `.md`;
   - вызвать `python3 scripts/radi_ct_workflow.py message <tmp.md>`;
   - вернуть Markdown-ответ в Telegram.

2. Если сообщение содержит `Исправляю case ...`:
   - извлечь `case_id`;
   - извлечь блоки `Финальный вариант`, `Почему`, `Теги`;
   - записать временные файлы final/feedback;
   - вызвать `correct`;
   - вернуть статус.

3. Если сообщение содержит `Принять case ...`:
   - вызвать `accept`;
   - вернуть статус.

4. Если сообщение содержит `Сохрани case ... как пример`:
   - вызвать `promote`;
   - если backend вернул PHI error, показать ошибку без обхода guard.

5. Если команда неполная:
   - задать короткий уточняющий вопрос;
   - не угадывать `case_id`, `area`, `final text`.

---

## Error handling

| Ошибка | Поведение Hermes |
|---|---|
| API недоступен | Сообщить, что локальный RadiCT API не запущен; предложить/запустить `uvicorn` только если это безопасно |
| Нет `case_id` | Спросить case_id |
| Empty final text | Попросить прислать финальный вариант |
| PHI guard при promote | Не обходить; попросить обезличить case |
| LLM/API error | Показать реальную ошибку; не выдумывать draft |

---

## Synthetic test conversation

```text
Роман → Telegram:
РКТ заключение
Область: ОГК
Контекст: синтетический обезличенный пример
Сравнение: да
---
Описание: синтетический очаг S8 правого легкого уменьшился. Плевра свободна.

Hermes → shell:
python3 scripts/radi_ct_workflow.py message /tmp/radi-ct-message.md

Hermes → Telegram:
RadiCT draft создан
Case ID: 2026-07-06-001
...

Роман → Telegram:
Исправляю case 2026-07-06-001

Финальный вариант:
Уменьшение очага S8 правого легкого. Плеврального выпота нет.

Почему:
- Указывать релевантные отрицательные стабильные находки.

Hermes → shell:
python3 scripts/radi_ct_workflow.py correct 2026-07-06-001 --final ... --feedback ... --create-lesson-candidate
```

---

## Current implementation status

Implemented:

- `scripts/radi_ct_api.py` — low-level HTTP CLI;
- `scripts/radi_ct_workflow.py` — Telegram/Hermes workflow wrapper;
- API endpoints for draft/accept/correct/cases/case/promote/lessons;
- unit tests for workflow parsing and API CLI;
- live smoke verified with synthetic no-PHI data.

Not implemented yet:

- automatic Hermes message routing rules inside this repository;
- robust PHI detection before draft/correct;
- true voice transcription pipeline;
- direct RadiProtocol UI integration.
