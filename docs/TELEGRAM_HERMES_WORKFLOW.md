# Telegram ↔ Hermes workflow для RadiCT Assistant

Документ описывает рекомендуемый Telegram-first workflow: Роман пишет команды в Telegram, Hermes распознаёт сценарий, вызывает локальный `radi-ct-assistant` через `scripts/radi_ct_workflow.py`, а затем возвращает результат в Telegram.

Это **не отдельный Telegram bot** внутри проекта. Telegram остаётся пользовательским интерфейсом, Hermes — оркестратор, `radi-ct-assistant` — локальный backend и хранилище learning-loop cases.

---

## Цели

1. Быстро тестировать RadiCT Assistant из Telegram без отдельного UI.
2. Сохранять draft/correct/feedback/promote события в локальный learning loop.
3. Не смешивать Telegram API, Hermes internals и FastAPI backend в одном слое.
4. Для радиологического диалога Романа автоматически сохранять accepted/corrected cases в few-shot reference base, если backend PHI guard не нашел прямых идентификаторов.
5. Минимизировать риск попадания PHI в local learning loop/reference base.

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

Принять draft и автоматически попытаться сохранить его в reference base:

```bash
python3 scripts/radi_ct_workflow.py accept CASE_ID
```

Сохранить исправление, создать lesson candidate и автоматически попытаться сохранить case в reference base:

```bash
python3 scripts/radi_ct_workflow.py correct CASE_ID \
  --final final.md \
  --feedback feedback.md \
  --tag incomplete_stable_findings_list \
  --create-lesson-candidate
```

Редкое исключение: принять/исправить без автосохранения в reference base:

```bash
python3 scripts/radi_ct_workflow.py accept CASE_ID --no-save-as-reference
python3 scripts/radi_ct_workflow.py correct CASE_ID --final final.md --no-save-as-reference
```

Отдельная команда `promote CASE_ID` остается для повторной ручной попытки, если автопромоушен ранее был заблокирован PHI guard или case был создан старым workflow.

Органичный захват обычной консультационной сессии, где Роман прислал исходное описание, Hermes предложил вариант, а затем Роман прислал финальный протокол/заключение:

```bash
python3 scripts/radi_ct_workflow.py capture-session session.md \
  --tag style_refinement \
  --create-lesson-candidate
```

`capture-session` одним вызовом создает draft case, сразу сохраняет финальный вариант как corrected case и по умолчанию пытается сохранить его в reference base. Если в тексте есть прямые идентификаторы, promotion блокируется backend PHI guard.

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

Hermes-only формат должен содержать входной текст и черновик ассистента. Если черновик передается отдельным файлом `--assistant-draft`, блок ниже можно не включать в message.md.

Формат:

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
| `Режим` | `fast` / `analytical` | metadata режима Hermes-черновика |
| `Тип ввода` | `markdown` / `text` / `voice_transcript` | источник/тип входного текста |

Блок `Черновик ассистента:` или отдельный `--assistant-draft` обязателен. Backend сохраняет case и не имеет fallback-генерации.

---

## Формат session-capture сценария

Когда радиологическая помощь идет как обычный диалог, а не как заранее созданный `case_id`, Hermes должен в конце сессии сам собрать `session.md` и вызвать `capture-session`, если есть:

- исходное описание/черновые находки;
- вариант ассистента или промежуточный черновик;
- финальный вариант Романа;
- отсутствие явных PHI-блокеров.

Формат `session.md`:

```text
РКТ заключение
Область: ОГК, ОБП, ОМТ
Контекст: травма, падение в смотровую яму
---
[исходное описание или черновые находки]

Черновик ассистента:
[вариант Hermes]

Финальный вариант:
[финальный протокол/заключение Романа]

Почему:
- [краткие причины правок/стилевые уроки]
```

Команда:

```bash
python3 scripts/radi_ct_workflow.py capture-session session.md \
  --tag conversational_capture \
  --create-lesson-candidate
```

Если вариант ассистента не был явно сохранен, допустимо использовать `--use-final-as-draft`, но это хуже для анализа ошибок: case будет полезен как reference, но не как сравнение draft→final.

Если пользователь явно сказал “не сохраняй”, добавляется `--no-save-as-reference` или команда не вызывается.

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

Hermes должен преобразовать это в команду ниже. В Telegram/Hermes workflow `correct` по умолчанию передает `save_as_reference=true`; backend выполнит PHI guard и отклонит promotion при прямых идентификаторах.

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

Hermes вызывает команду ниже. В Telegram/Hermes workflow `accept` по умолчанию передает `save_as_reference=true`; backend выполнит PHI guard и отклонит promotion при прямых идентификаторах.

```bash
python3 scripts/radi_ct_workflow.py accept 2026-07-06-001
```

Если пользователь явно просит не сохранять пример, Hermes добавляет `--no-save-as-reference`.

---

## Формат promote-сценария

В радиологическом Telegram/Hermes workflow few-shot reference base пополняется автоматически после `accept`/`correct`, если backend PHI guard пропускает case. Команда `promote` нужна для ручной повторной попытки или старых cases.

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

### Нельзя автоматически сохранять в long-term reference base

Остановиться и попросить обезличить текст перед сохранением как reference, если есть:

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

В радиологическом Telegram/Hermes workflow `accept` и `correct` по умолчанию автоматически пытаются сохранить case в reference base. Backend PHI guard остается обязательным предохранителем: если он находит прямые идентификаторы, promotion отклоняется, но corrected/accepted case и feedback остаются в локальном learning loop.

Если нужно разово не сохранять пример, использовать `--no-save-as-reference`.

---

## Recommended Hermes behavior

Когда Hermes получает Telegram-сообщение:

1. Если сообщение начинается с `РКТ заключение`, `РКТ описание` или `РКТ описание + заключение`:
   - проверить PHI-risk для long-term сохранения;
   - получить/собрать локальный RAG-контекст из references при необходимости;
   - сформировать черновик самим Hermes Agent;
   - записать во временный `.md` входной текст + блок `Черновик ассистента:`;
   - вызвать `python3 scripts/radi_ct_workflow.py message <tmp.md>`;
   - вернуть Markdown-ответ в Telegram.

2. Если сообщение содержит `Исправляю case ...`:
   - извлечь `case_id`;
   - извлечь блоки `Финальный вариант`, `Почему`, `Теги`;
   - записать временные файлы final/feedback;
   - вызвать `correct` без `--no-save-as-reference`, чтобы автоматически попытаться сохранить reference;
   - если backend вернул PHI error на promotion, показать ошибку без обхода guard; corrected case и lesson candidate при этом остаются сохраненными.

3. Если сообщение содержит `Принять case ...`:
   - вызвать `accept` без `--no-save-as-reference`, чтобы автоматически попытаться сохранить reference;
   - если backend вернул PHI error на promotion, показать ошибку без обхода guard.

4. Если сообщение содержит `Сохрани case ... как пример`:
   - вызвать `promote`; это ручная повторная попытка для старого case или ранее заблокированного promotion;
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
| Missing assistant draft | Hermes должен сформировать черновик и повторить `message`; backend не генерирует сам |

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
