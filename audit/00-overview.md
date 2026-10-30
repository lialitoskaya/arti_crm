# Этап 0. Обзор проекта

Дата: 2026-10-30. База аудита: коммит `38b34e3` (production, ветка `feat/ozon-fbo-supplies`).

## Стек

| Слой | Что используется |
| --- | --- |
| Backend | Python 3.13, FastAPI 0.115.6, uvicorn 0.34 (локально), на хостинге `server.py` → `a2wsgi.ASGIMiddleware` + `wsgiref` под pm2; `passenger_wsgi.py` для Passenger |
| БД | SQLite, схема и «лёгкие миграции» в `app/db.py:init_db()` (запускается при старте). Отдельной команды миграций нет |
| Frontend | Без фреймворка и без сборщика: `app/static/index.html` (1211 строк), `app/static/app.js` (9731 строка, один файл, 483 функции верхнего уровня), `app/static/styles.css` (6484 строки). Плюс `theme-init.js` (анти-FOUC тема), `sw.js` (push-уведомления, без кэширования ресурсов), `manifest.webmanifest`, иконки PNG/SVG |
| Стили | Чистый CSS, кастомные свойства `--crm-*`. Шрифт Onest подключается с Google Fonts (внешний запрос, разрешён в CSP) |
| UI-библиотека | Нет. Все компоненты собственные |
| Роутинг | Hash-роутинг внутри SPA: `#chats`, `#chats/<id>`, `#analytics`, `#supply`, `#fbo`, `#tasks`, `#reviews`, `#questions`, `#questions/<id>`, `#knowledge`, `#users`, `#settings`, `#profile`. Переключение — `showView()` (`app.js:7283`), все разделы присутствуют в DOM постоянно и скрываются классом `.hidden` |
| Линтеры/типы | Не настроены (ни ruff/flake8/mypy, ни eslint/stylelint). Node 24 есть на машине и используется только тестами |
| Тесты | `pytest` (`.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q`), 47 модулей. UI-тесты — характеризационные: вырезают функции из `app.js`, запускают через `node`, проверяют строки разметки/стилей и совпадение версии кэша (`sw.js` ↔ `index.html`) |
| Запуск dev | `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`; статику отдаёт backend (`/` → `index.html`, `/static/*`) |
| Сборка | Отсутствует. Версионирование ресурсов — вручную через `?v=v96-…` в `index.html`, `ARTI_CRM_SW_VERSION` в `sw.js` и `<meta name="arti-build-version">` |

## Карта разделов

| Маршрут | `main#…` в `index.html` | Загрузчик в `app.js` | API | Доступ |
| --- | --- | --- | --- | --- |
| `#chats` | `chatsView` (122–317) | `loadChats`, `openChat`, sync-цикл | `/api/chats*`, `/api/sync/*`, `/api/stats` | все · **исключено из работы** |
| `#analytics` | `analyticsView` (318–440) | `loadAnalytics` (6360) | `/api/analytics/chats`, `/api/analytics/chats/drilldown?limit=1000` | все |
| `#supply` | `supplyPlanningView` (441–531), «Поставки (beta)» | `loadSupplyPlanning` (7022), состояние в `localStorage` | `/api/supply-planning/sync` | все |
| `#fbo` | `ozonFboView` (532–710) | `loadOzonFbo` (9148), опрос раз в 2 с во время прогона | `/api/ozon-fbo/*` | admin |
| `#reviews` | `reviewsView` (711–763) | `loadReviews` (4907) | `/api/reviews*` | все |
| `#questions` | `questionsView` (764–815) | `loadQuestions` (5263) + `runFrontendQuestionsSyncSoon` | `/api/questions*` | все |
| `#tasks` | `tasksView` (816–895) | `loadTaskTypes` → `loadAllTasks` (5755) | `/api/tasks?…`, `/api/task-types`, `/api/users/assignees` | все |
| `#knowledge` | `knowledgeView` (896–971) + `#knowledgeModal` (1188) | `loadKnowledge` (6154) | `/api/knowledge/*` | все; создание — admin |
| `#users` | `usersView` (972–1055) + модалки создания/редактирования | `loadUsers` (7883) | `/api/users*` | admin |
| `#settings` | `techSettingsView` (1056–1157): статусы чатов, типы задач, связи Яндекс OAuth | `loadChatSettings`, `loadTaskTypes`, `loadYandexOAuthManagedLinks` | `/api/chat-settings*`, `/api/task-types*`, `/api/admin/yandex-oauth-links*` | admin |
| `#profile` | `profileView` (1158–1187) | `fillProfileForm` | `PATCH /api/auth/profile` | все |
| — | `loginScreen` (33–44), `authBootScreen` (23–31) | `checkAuth`, `showLogin` | `/api/auth/*` | публично |
| — | Shell: `.app-nav` (47–89), лист «Ещё» для мобильных (93–119), уведомления (`#notificationsBtn`, панель, тосты `#notificationToasts`) | `loadNotifications` каждые 10 с, `loadSyncStatus` раз в 60 с, `refreshVisibleData` раз в 60 с | `/api/notifications*`, `/api/sync/status` | все |

Общие для всех разделов: хедер-«hero» `.tasks-toolbar.panel-card` (заголовок + описание), карточки
`.panel-card`/`.settings-card`, таблицы `.analytics-table`, бейджи `.status-badge`, модалки
`.user-create-modal` (сотрудники, решение FBO), календарный фильтр `.date-range-filter-*` (чаты и задачи),
`notify()` → тосты, `.empty-state`.

## Токены дизайна и темы

- Базовый набор `:root` — `styles.css:4–47`: фон/панели, линии, текст (`--crm-text #222`, `--crm-text-soft`,
  `--crm-muted #777`, `--crm-muted-2 #a0a0a0`), семантические пары `--crm-{blue,green,red,orange,purple,yellow}` +
  `-text`, тени `--crm-shadow-{xs,sm,md}`, радиусы `--crm-radius-{xs 5,sm 7,md 10,lg 14}`, ширины
  `--crm-sidebar 214px`, `--crm-chat-list 404px`, `--crm-focus`.
- Слой v52 (`styles.css:3272–3349`) **переопределяет те же имена** для `html[data-theme="light"]` и `html[data-theme="dark"]`
  (акцент `--crm-primary` становится синим `#2563eb`/`#63a2ff`, текст `#162033`/`#edf3ff`, muted `#7a879b`/`#8b98ab`).
  В light-слое `--crm-bg: #ffffff0000` — некорректный 8-значный hex (полностью прозрачный белый).
- Фон `body` в тематическом режиме — радиальные градиенты + линейный градиент (`3351–3363`), панели полупрозрачные
  `rgba(255,255,255,.92)`; `.app-nav` — `rgba(255,255,255,.64)`.
- Тема применяется атрибутом `html[data-theme]` (`theme-init.js` до загрузки CSS), переключатель в навигации.
- Типографика: `body` 14px / 1.35, Onest. В CSS 114 объявлений `font-size` в px, из них 57 — 12px и меньше.
- Отдельные локальные токены: `--knowledge-category-*` (база знаний), `--crm-chat-*` (чат), `--crm-button-*`
  (кнопки в dark-слое).

## Общие компоненты (где определены)

| Компонент | Определения |
| --- | --- |
| Кнопки | `button`, `.button-like`, `.secondary/.ghost/.crm-light-btn/.user-light-btn/.refresh-btn`, `.link-btn`, `.small`, `.icon-btn` (`styles.css:67–130`), плюс dark-слой 4195–4385 и переопределения в разделах. Три разных класса «светлой кнопки» для одного смысла |
| Поля ввода | `input/select/textarea` базово (`font: inherit`), конкретные стили — в блоках разделов и dark-слое 4009–4038; `::placeholder` и `:focus-visible` — см. этап 1 |
| Карточки | `.panel-card`, `.settings-card`, `.analytics-card` |
| Таблицы | `.analytics-table` (аналитика, drilldown), grid-«таблицы» в настройках статусов/типов задач (`.status-settings-*`, `.task-type-settings-*`), строки поставок |
| Модалки | `.user-create-modal*` (сотрудники, FBO), `.knowledge-modal*`, `.mobile-more-sheet` |
| Селекты | нативные `<select>`; выбор цвета статуса — emoji-опции 🟠🔵🟣 (`index.html:1076–1081`) |
| Уведомления | `notify()` → `#notificationToasts`, панель уведомлений в навигации, звук, Web Push |
| Иконки | 34 inline SVG в `index.html` (одни и те же path повторяются для desktop-навигации и листа «Ещё»), 6 файлов `icons/nav-*.svg`, текстовые глифы `×`, `←`, `✕`, `⋯`, `＋`, `✦`, `✎` в кнопках |

## Backend-структура, важная для аудита производительности

- `app/main.py` (6087 строк): все маршруты, 4 middleware, фоновые циклы. Порядок middleware: `no_cache_frontend`
  (внешний) → `add_fastfox_cache_headers` → `require_auth_for_api` → `security_guard_middleware`.
- Статика: `StaticFiles` с проверкой пути; заголовки для `/static/*` выставляются дважды — `public, max-age=3600`
  и затем перезаписываются на `no-store` (`main.py:776–782`). Сжатия ответов в приложении нет.
- Списки: `list_tasks` (JOIN + отдельный запрос комментариев на каждую задачу), `list_reviews`/`list_ozon_questions`
  (`LIMIT 500`, сортировка по `datetime(COALESCE(...))`), `list_knowledge_articles` (без лимита), analytics drilldown
  (`limit=1000`, `include_excluded=1`).
- Индексы (`db.py:1047–1095`) есть для messages/chats/tasks/notifications/push/reply_templates; для `reviews`,
  `ozon_questions`, `knowledge_articles` индексов нет.

## Размер фронтенда (без сборки, как отдаётся клиенту)

| Файл | Байт | gzip -9 |
| --- | --- | --- |
| `app.js` | 398 338 | 88 105 |
| `styles.css` | 192 545 | 29 746 |
| `index.html` | 86 988 | 16 649 |
| Итого первой загрузки (без шрифтов и иконок) | 677 871 | 134 500 |

Плюс Google Fonts (CSS + 4 начертания woff2), `icons/Gradient-Mesh-27.jpg` (25 КБ; использование проверяется на этапе 2),
6 SVG навигации, PNG иконки приложения.

## Что уже известно по качеству кода (детали — этап 3)

- `!important` в CSS: 481; правил с префиксом `html[data-theme]`: 315; CSS организован как последовательность
  версионных слоёв v16…v96 поверх базы (комментарии-заголовки по версиям).
- `app.js`: 86 присваиваний `innerHTML`, 94 `addEventListener`, 6 `setInterval`, 57 `console.warn/error`, один
  глобал на `window` (`__kbSearchTimer`), `CRM_BUILD_VERSION` в `main.py` = `v80-…` при `v96-…` в `index.html`.
- Корень репозитория содержит 90+ файлов `UPDATE_V*_README.txt`, `docs/V*.md` — исторические заметки релизов.

## Границы работы

- Раздел чатов исключён — см. `audit/chat-exclusions.md`.
- Бизнес-логика, схема БД, API-контракты и права не меняются. Найденные баги логики фиксируются в
  `audit/06-found-bugs.md`.
- Файл `patch19.patch` в корне — пользовательский, не трогать. `.env` и базы не читаются.
- Инструменты для измерений: встроенный браузер (DevTools-метрики по сети и консоли), размеры файлов. Lighthouse
  и bundle analyzer недоступны (нет сборки и внешних пакетов) — это ограничение фиксируется в `02-performance.md`.
