# Тестирование CRM

## Назначение

Regression foundation фиксирует существующее поведение перед дальнейшим
рефакторингом. Тесты не меняют production-код, не используют реальные credentials,
пользовательские данные, production SQLite или API маркетплейсов.

## Установка

Test dependencies отделены от production dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

Не добавляйте pytest в `requirements.txt`: этот файл предназначен для runtime
приложения.

## Запуск

Основная команда:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Эта команда запускает и классы `unittest`, и тесты с фикстурами `pytest`.
Правила доступа по разделам проверяют `tests/test_section_permissions.py` и
`tests/test_route_rbac.py`.

### Проверки интерфейса

Полная проверка перед публикацией включает pytest и JavaScript-сценарии ниже.
Для браузерных сценариев нужны Node.js, доступный для `require('playwright')` пакет Playwright и установленный Microsoft Edge. Запускайте из корня репозитория.

```powershell
node tests/test_analytics_lazy_loading.cjs
node tests/test_analytics_lazy_browser.cjs
node tests/test_chat_search_layout.cjs
node tests/test_section_permissions_browser.cjs
node tests/test_operator_views_browser.cjs
```

| Если меняется | Где проверять регрессию |
| --- | --- |
| Версия статических файлов | `test_chat_task_visual_contract.py` читает версионные ссылки из HTML и сверяет их с исполняемым service worker. Этой проверки достаточно для всех разделов. |
| Отправка подписи и вложений | `test_message_send_outbox_ui.py` проверяет ожидание подтверждения и блокировку отправки файла после неудачи. `test_operator_views_browser.cjs` отправляет форму, имитирует потерянный ответ и проверяет единственный POST, поиск исходной операции и содержимое второй, отдельной отправки. |
| Шаблоны, отзывы и карточка товара в чате | `test_operator_views_browser.cjs` применяет шаблон, открывает отзыв из списка и проверяет ссылку на товар после обновления переписки. |
| Персональные отметки чата | `test_chat_pin_state_ui.py` проверяет откат после ошибки, в том числе при одновременной смене отметки прочтения. `test_chat_task_visual_contract.py` проверяет результат рендера и зарегистрированные обработчики. |
| Доступ к FBO | `test_operator_views_browser.cjs` открывает раздел с правом просмотра. `test_section_permissions_browser.cjs` проверяет отказ для сотрудника без этого права. Серверные ограничения отдельно проверяют `test_ozon_fbo_router.py` и `test_section_permissions.py`. |
| Ошибки входа | `test_auth_login_errors.py` проверяет сообщения для кодов входа по паролю и через Яндекс. `test_yandex_oauth.py` проверяет ответ сервера, а браузерный сценарий проверяет, что форма показывает безопасный текст при неизвестной ошибке. |

## Границы безопасности

Безопасный bootstrap выполняется до первого `import app.*`:

- environment процесса заменяется минимальным test-only набором; из исходного
  окружения сохраняются только `PATH`, `PATHEXT` и системные переменные Windows
  (`SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`). `PATH` общий для всего процесса, без
  него тесты, запускающие `node` через `subprocess`, проходят поодиночке, но
  падают в полном прогоне после импорта foundation другими модулями;
- `dotenv.load_dotenv` подменяется no-op, поэтому `.env` не читается;
- `DATABASE_PATH`, attachments и state/cache paths направляются во временный
  каталог;
- SQLite guard разрешает подключение к выбранной временной БД и к SQLite в памяти (`:memory:`);
- временные SQLite/WAL/SHM-файлы удаляются после каждого теста;
- socket guard блокирует внешние соединения;
- HTTP smoke requests используют только in-memory `httpx.ASGITransport`;
- FastAPI startup/lifespan не запускается;
- fixtures синтетические и обезличенные;
- legacy-файлы из `chat_attachments/` не используются.

Если тест пытается открыть другую SQLite или выполнить network connection, он
завершается ошибкой.

## Текущее покрытие

Набор `tests/test_regression_foundation.py` фиксирует:

1. import приложения в изолированном environment без создания БД;
2. offline smoke behavior для `/health`, `/` и
   `/static/manifest.webmanifest`;
3. прямую viewer/admin границу `_require_admin`;
4. последовательную идемпотентность `repository.add_message` для одинакового
   external message ID: одна строка, тот же ID и обновлённый preview;
5. сохранение исходного `PATH` в изолированном environment: `shutil.which("node")`
   после bootstrap даёт тот же результат, что и до него.

Раздел «Поставки FBO» покрыт отдельными модулями: `tests/test_ozon_fbo_core.py`
(правила, предохранители, расчёт артикула, боевой поток, httpx-клиент через
`MockTransport`), `tests/test_ozon_fbo_pipeline.py` (конвейер, общий план, грузоместа,
кластер «все», дубли — на подменённом Ozon и хранилище в памяти),
`tests/test_ozon_fbo_repository.py` (таблицы, настройки, единственный активный прогон,
вопрос/ответ, forward recovery), `tests/test_ozon_fbo_runner.py` (поток прогона с
состоянием в SQLite), `tests/test_ozon_fbo_router.py` (HTTP-контракт и RBAC через
in-memory ASGI-клиент) и `tests/test_ozon_fbo_ui.py` (характеризация разметки,
скриптов, стилей и версии кэша).

## Осознанные ограничения

### Наблюдения по результатам обзора основного кода CRM

| Участок | Сценарий для отдельного исправления и проверки |
| --- | --- |
| Очередь push-уведомлений, `repository._ensure_push_storage_conn` | Подготовка таблиц через SQLite `executescript` может завершить транзакцию вызывающего кода. На SQLite в памяти запись, сделанная до вызова, сохранилась после отката. При исправлении проверить откат сообщения, уведомления и записи очереди при сбое в середине операции. |
| Учёт неудачных входов, `repository.record_login_failure` | При записи очередной ошибки во время уже действующей блокировки одна ветка использует `first_failed_iso` до присваивания. При проверке важны исходное время первой ошибки и срок блокировки. |
| Вопросы Ozon, `repository.enqueue_missing_question_notifications` | Список вопросов и фоновое создание пропущенных уведомлений распознают разные наборы конечных статусов. Сверить результат для недавнего закрытого вопроса без текста ответа. |
| Черновики ответов, `submitQuestionAnswer` в `app.js` | Форма настольной версии после ожидания ответа сервера снова читает выбранный вопрос. Если оператор успел перейти к другому вопросу, обработчик может очистить его черновик. Проверять с задержкой ответа и двумя разными черновиками. |

Для переноса планирования поставок и импорта событий Wildberries нужно предварительно зафиксировать результат расчётов по каждому маркетплейсу, работу кэша и возобновление импорта по сохранённому курсору. Проверки прав на API покрывают отдельную часть этих сценариев.

### Ограничения базового набора

Foundation пока не проверяет:

- FastAPI startup/lifespan, migrations, cleanup/repair jobs и background tasks;
- полную login/session middleware chain;
- конкурентную доставку одинакового external message ID — принятый baseline не
  содержит DB unique constraint для этого инварианта;
- contract behavior Ozon, Wildberries и Yandex;
- реальные HTTP, marketplace credentials и production data;
- binary fixtures;
- произвольный порядок модулей: `tearDownModule` foundation после собственных
  тестов снимает все process-wide патчи (environment, dotenv no-op, network и
  SQLite guards) и удаляет временный каталог. Полный алфавитный прогон проходит
  лишь потому, что промежуточные модули заново создают каталог через
  `init_db()`; запуск зависимого модуля сразу после foundation (например,
  `pytest tests/test_regression_foundation.py tests/test_runtime_startup.py`)
  падает. Это известный долг изоляции, а не гарантия порядка.

Если новый тест требует изменения production-кода, вынесите такое изменение в
отдельную задачу и отдельный commit.
