# CRM Engineering Instructions

## Цель

Поддерживать CRM как долгоживущую production-систему. Приоритеты: корректность, целостность данных, безопасность, тестируемость, понятная архитектура и безопасные небольшие изменения.

## Обязательные границы безопасности

- Никогда не читать, не запрашивать, не печатать, не копировать и не коммитить production-секреты.
- Не читать `.env`, `.env.*`, приватные ключи, cookies, credential-файлы, production-дампы и сырые клиентские логи.
- Не выводить все переменные окружения и authorization headers.
- Не подключаться к production БД, Redis, очередям или API маркетплейсов.
- Работать только в local/test/mock/replay среде с обезличенными fixtures.
- Не включать сеть и не менять sandbox/permissions без явной задачи владельца.
- Не добавлять telemetry или внешние сервисы без явного согласования.

## Git-безопасность

- Работать в отдельной ветке и сначала проверять `git status`.
- Не удалять и не перезаписывать пользовательские изменения.
- Запрещены `git reset --hard`, `git clean -fd`, force push и переписывание истории.
- Не смешивать в одной задаче новую функцию, массовое форматирование, upgrade зависимостей и рефакторинг.
- Не делать commit без явного запроса.
- Перед завершением проверять полный diff и отсутствие секретов/генерируемых файлов.

## Обязательный процесс

Для багов, рефакторинга, интеграций, БД, синхронизации и изменений нескольких модулей:

1. Прочитать применимые `AGENTS.md` и документы в `docs/`.
2. Проследить полный текущий поток данных и выполнения.
3. Найти самую раннюю неправильную точку или нарушение архитектурной границы.
4. Зафиксировать ожидаемое поведение, бизнес-инвариант и источник истины.
5. Добавить/определить characterization и regression tests.
6. Составить ограниченный план и список разрешённых файлов.
7. Реализовать один вертикальный срез.
8. Запустить целевые и общие проверки.
9. Проверить полный diff.
10. Провести независимый review.
11. Исправить блокирующие замечания и повторить review.
12. Обновить документацию.

Запрещено начинать с широкого переписывания. Запрещено скрывать первопричину фильтром в UI. Запрещено одновременно запускать двух write-агентов на одном working tree.

## Роли агентов

- `architect`: только анализ; не меняет файлы.
- `implementer`: выполняет только утверждённый ограниченный план.
- `reviewer`: независимо проверяет diff; сам ничего не исправляет.
- Агент, написавший код, не может быть единственным, кто его одобрил.

## Архитектурное направление

```text
API route/controller
  -> application service/use case
    -> repository and/or marketplace integration
      -> database / external API
```

Правила:

- Route/controller валидирует транспортный ввод, вызывает service и формирует ответ.
- В route запрещены SQL, бизнес-процессы, retry и marketplace mapping.
- Бизнес-оркестрация находится в services/use cases.
- Доступ к БД находится в repositories.
- HTTP, payload mapping, внешние статусы и ошибки маркетплейса находятся в `integrations/<marketplace>/`.
- Repository не вызывает API маркетплейса; integration client не пишет напрямую в БД.
- Domain/business rules не зависят от web framework, DB session и marketplace SDK.
- Frontend не исправляет повреждённые backend-данные.
- Избегать глобального mutable state, циклических импортов и дублирующей нормализации.
- Статусы оформлять типами/enums, а не рассыпанными строками.

## Целостность данных CRM

- Для каждой внешней сущности определить устойчивую identity strategy.
- Webhook, polling, import, retry и sync должны быть идемпотентными.
- Повтор одного события не создаёт вторую запись и второй side effect.
- Бизнес-уникальность защищать ограничением БД, когда это возможно.
- Проверки `if not exists -> insert` недостаточно для конкурентной безопасности.
- Явно задавать transaction boundaries и обработку unique conflicts.
- Старое/задержанное событие не должно откатывать более новое состояние.
- Optimistic local entity связывать с внешней через устойчивый correlation/client request ID.
- Не дедуплицировать только по тексту и близкому времени при возможности устойчивого ID.
- Не удалять дубли постфактум как основное исправление — предотвращать создание.
- Изменения схемы только через миграцию, тест, rollout и rollback/forward recovery.

## Интеграции маркетплейсов

- Ozon, Wildberries, Yandex и другие адаптеры изолированы друг от друга.
- Не создавать ложную общую модель для различающейся семантики.
- Валидировать и маппить внешние payload на границе интеграции.
- Определять timeout, retry/backoff, rate limits и классы ошибок.
- Retry обязан быть безопасным и идемпотентным.
- Не логировать токены, cookies, полные headers, адреса, телефоны и сырые личные сообщения.
- Contract tests используют mocks или обезличенные записанные fixtures.

## Правила backend

- Маленькие модули с одной ответственностью.
- Новому и существенно изменённому публичному коду — type annotations.
- Не использовать пустой/broad `except` без осмысленной обработки.
- Не подавлять ошибки молча.
- Не добавлять production-зависимость без объяснения и согласования.
- I/O держать на границах; бизнес-логику делать максимально детерминированной.

## Правила frontend

- Разделять API transport, server state, local UI state и presentation.
- Backend остаётся источником истины бизнес-правил.
- Двойную отправку предотвращать operation/request ID, а не только disabled-кнопкой.
- Optimistic updates имеют явные reconciliation и rollback.
- Не логировать секреты или сырые приватные payload в браузере.

## Обязательный UI/UX gate

Для любой задачи, затрагивающей HTML, CSS, JavaScript-rendering, компоненты,
адаптивность, визуальные состояния или пользовательские сценарии, до анализа и
изменения кода обязательно полностью прочитать перечисленные документы. Это
требование действует и для локальной правки одной кнопки или одного CSS-правила:

- `docs/UI_DESIGN_STANDARDS.md`
- применимые разделы `docs/DEVELOPMENT_WORKFLOW.md`

`docs/UI_DESIGN_STANDARDS.md` является обязательным production-стандартом проекта,
а не рекомендацией. Его требования применяются к новой функциональности,
редизайну, локальным визуальным правкам и review чужого UI-diff.

Перед реализацией UI-задачи исполнитель обязан зафиксировать:

1. пользовательскую проблему и рабочий сценарий;
2. текущую первопричину визуальной или UX-проблемы;
3. существующие компоненты, токены и паттерны, которые будут переиспользованы;
4. старую разметку, стили, обработчики или состояние, которые будут удалены;
5. почему решение не создаёт второй конкурирующий UI-путь.

Запрещено:

- делать универсальный «AI dashboard» вместо интерфейса конкретной CRM;
- добавлять glassmorphism, декоративные градиенты, свечение, большие pill-кнопки,
  чрезмерные скругления, карточки внутри карточек и пустые декоративные области без
  подтверждённой продуктовой необходимости;
- создавать новые CSS override-слои поверх ошибочной реализации;
- дублировать компоненты, selectors, обработчики, design tokens и источники UI-state;
- скрывать важные действия только через hover;
- снижать информационную плотность рабочих списков ради декоративной «чистоты»;
- завершать UI-задачу без проверки desktop, mobile, длинного контента, empty/loading/
  error/disabled states, клавиатуры, доступности и DOM/performance-регрессий.

Для UI-review обязательны before/after материалы и заполненный UI-раздел в PR
шаблоне. Reviewer сверяет результат с `docs/UI_DESIGN_STANDARDS.md` и блокирует
изменение при наличии AI-slop паттернов, дублирующей реализации или необоснованного
расхождения с текущей дизайн-системой.

## Рефакторинг

- Сначала зафиксировать поведение тестами, затем переносить.
- Рефакторить вертикальными бизнес-срезами, а не механически делить большой файл.
- Formatting-only изменения отделять от semantic changes.
- Новый путь -> миграция ограниченных consumers -> проверка -> удаление старого пути.
- Временный compatibility path имеет условие удаления.
- Не создавать `v2`, `fixed`, `new`, `final` без плана миграции и удаления.

## Тесты

Приоритет:

1. Regression tests известных багов.
2. Characterization tests legacy-поведения.
3. Unit tests бизнес-правил.
4. Integration tests БД/транзакций.
5. Contract tests marketplace adapters.
6. E2E tests критических пользовательских потоков.
7. Concurrency/idempotency tests синхронизации и сообщений.

Для багфикса тест должен воспроизводить старую ошибку и оставаться после исправления. Проверять повторную доставку, retry после timeout, reordered events, partial failure и конкурентный запуск, когда это применимо.

## Definition of Done

Задача завершена только когда:

- описаны ожидаемое поведение и root cause/архитектурная цель;
- есть необходимые regression/characterization tests;
- целевые и релевантные общие тесты проходят;
- lint/format/typecheck проходят либо честно указано, почему недоступны;
- миграция и восстановление проверены, если затронута БД;
- diff не содержит секретов и несвязанных изменений;
- reviewer не оставил blocker/high замечаний;
- документация обновлена;
- перечислены изменённые файлы, команды и фактические результаты.

## Документы проекта

Поддерживать:

- `docs/ARCHITECTURE.md`
- `docs/BUSINESS_RULES.md`
- `docs/SECURITY_BOUNDARIES.md`
- `docs/UI_DESIGN_STANDARDS.md`

## PROJECT-SPECIFIC

- Backend root: `app`
- Frontend root: `app/static`
- Backend start command: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Frontend start command: `Not configured — frontend is served by the backend`
- Unit test command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q`
- Integration test command: `Not configured`
- E2E command: `Not configured`
- Lint command: `Not configured`
- Typecheck command: `Not configured`
- Migration tool/command: `No standalone migration command — app.db.init_db() runs inline schema initialization/lightweight migrations at application startup`
- Local test DB: `temporary SQLite database created by pytest tmp_path`
- Critical entry points: `app/main.py:app`, `app/main.py:on_startup`, `app/main.py:_background_sync_loop`, `app/main.py:_sync_marketplace_locked`, `app/main.py:_sync_ozon_fast_inbox_locked`, `app/main.py:send_message`, `app/db.py:init_db`, `app/repository.py:add_message`, `app/connectors/ozon.py:OzonConnector`, `app/static/index.html`, `app/static/app.js:bootstrap`, `server.py:application`, `passenger_wsgi.py:application`
