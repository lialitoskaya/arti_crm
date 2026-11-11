# Этап 2. Аудит производительности (без правок)

База: `38b34e3`. Полный отчёт с доказательствами `file:line` — `audit/details/performance-full.md`.
Здесь — сводка, фактические замеры «до» и ограничения метода.

## Ограничения метода

- Сборки и bundle analyzer нет: фронтенд отдаётся тремя файлами как есть. «Размер бандла по чанкам» = размер
  файлов `app/static/*` (ниже).
- Lighthouse недоступен (нет Node-пакетов, сеть для установки не включалась по правилам проекта). Вместо него:
  заголовки и размеры ответов (`curl`), журнал сети/консоли встроенного браузера, замеры Navigation Timing /
  Paint Timing / размера DOM через DevTools-протокол на локальном стенде (`audit/screenshots/before/*-report.json`,
  поле `timing`). Локальный стенд не отражает сетевую задержку хостинга: ценность цифр — в относительном
  сравнении «до/после» и в счётчиках (байты, запросы, узлы DOM), а не в абсолютном времени.
- Данные стенда синтетические и маленькие; запросы, чья стоимость растёт с объёмом (`/api/reviews` 500 строк,
  аналитика по всем `messages`), оценены по коду, а не по времени ответа.

## Замеры «до»

### Статика (один холодный вход)

| Файл | Байт | gzip -9 | Заголовки фактически (`curl -I`, стенд) |
| --- | --- | --- | --- |
| `index.html` | 86 988 | 16 649 | `cache-control: no-store, no-cache, must-revalidate, max-age=0` |
| `styles.css` | 192 545 | 29 746 | то же; `content-encoding` отсутствует |
| `app.js` | 398 338 | 88 105 | то же; `etag` и `last-modified` есть, но при `no-store` не используются |
| `theme-init.js` | 501 | — | блокирующий `<script>` в `<head>` |
| `icons/nav-*.svg` ×6 | 5 690 | — | 6 запросов, `no-store` |
| Google Fonts CSS + ≈8 woff2 | внешние | — | render-blocking CSS с третьей стороны |
| **Итого статики** | **≈690 KB, 10 запросов** | **≈135 KB** | повторяется при каждом входе/F5 |

Причина: `no_cache_frontend` (`app/main.py:776–782`) — внешний middleware, перезаписывает `public, max-age=3600`
из `add_fastfox_cache_headers` (`512–530`). `GZipMiddleware` отсутствует. Версионирование `?v=` уже есть, то есть
`/static/*` можно отдавать с длинным кэшем, а `index.html` оставить `no-store`.

### Запросы API на стенде (данные маленькие)

| Endpoint | Байт | Время |
| --- | --- | --- |
| `/api/tasks` (13 задач, с комментариями) | 15 427 | 7 мс |
| `/api/reviews` (14 отзывов, с `raw`) | 10 965 | 6 мс |
| `/api/questions` (12 вопросов, с `raw`) | 8 023 | 5 мс |
| `/api/knowledge/articles` (7 статей, полный `content`) | 15 223 | 5 мс |
| `/api/analytics/chats` (14 дней) | 7 043 | 18 мс |
| `/api/analytics/chats/drilldown?limit=1000&include_excluded=1` | 19 120 | 9 мс |
| `/api/notifications?limit=30` | 29 | 9 мс |
| `/api/stats` | 243 | 6 мс |
| `/api/chat-settings` / `/api/task-types` / `/api/users/assignees` | 1 819 / 1 001 / 344 | 5 мс |

На production размеры растут линейно с `LIMIT 500` и полным `raw_json` маркетплейса в каждой строке отзывов и вопросов;
без сжатия это десятки–сотни KB на запрос.

### Фоновые запросы простаивающей вкладки

Журнал сети встроенного браузера: 49 запросов к `/api/` за ≈90 с после входа при открытой «Аналитике»
(повторяющиеся `GET /api/chats?paginated…` ×8, `POST /api/sync/operator` ×7, `POST /api/questions/sync/ozon` ×5 +
`GET /api/questions?unanswered=true` ×5, `GET /api/notifications` ×7, `GET /api/stats` ×4, `GET /api/sync/status` ×4).

| Таймер (`app.js`) | Интервал | Что делает вне чатов |
| --- | --- | --- |
| `notificationsTimer` (8953) | 10 с | `GET /api/notifications?limit=30` |
| `alertsTimer` (2782) | 10 с | ещё раз `loadNotifications()` + `GET /api/chats?paginated` |
| `frontendSyncTimer` (2761) | 5 с | `POST /api/sync/operator` (gap 5 с) + `POST /api/questions/sync/ozon` (gap 12 с) → `GET /api/questions` (500 строк) → `GET /api/stats` |
| `statusTimer` (8950) | 60 с | `GET /api/sync/status` |
| `refreshTimer` (8947) | 60 с | `refreshVisibleData()`: аналитика — 2 тяжёлых запроса + stats + status; задачи — tasks + stats + status; отзывы — reviews + openReview + stats + status; знания — categories + articles |

≈40 запросов/мин на вкладку без действий пользователя; каждый `POST` пишет строку в `audit_logs` (`main.py:356–372`)
без retention. Таймеры `frontendSyncTimer`/`alertsTimer` лежат в исключённом блоке чатов (`app.js:2661–2841`) — их
изменение требует согласования; `notificationsTimer`/`refreshTimer` — вне чатов.

### Запросы при входе в раздел

Из `before-report.json` (`api_requests`): задачи — 11–13 запросов на холодный вход (`auth/me`, `security/csrf`,
`chat-settings`, `task-types` ×2, `users/assignees` ×2, `sync/status`, `notifications`, `tasks`, `sync/operator`);
тех. настройки — 14 (`chat-settings` и `task-types` запрашиваются повторно, хотя уже загружены в `init()`).

### DOM и рендеринг

- Статический DOM всех 11 разделов: 1 126 элементов (не проблема).
- Динамические списки пересобираются целиком `innerHTML` на каждый poll/фильтр без сравнения с предыдущими данными:
  `renderAllTasks` (≈35 узлов/карточка, панель редактирования и меню статусов создаются для каждой карточки заранее),
  `renderReviewsList`/`renderQuestionsList` (до 500 строк; вопросы — каждые ≈12 с), `renderAnalyticsDrilldown`
  (≈15 000 узлов + 1 000 `addEventListener` каждую минуту), `renderKnowledgeCategories` (дважды на загрузку и на
  каждый `resize`).
- `handleSupplyPlanningInput`: `JSON.stringify` всех строк + синхронная запись `localStorage` на каждый символ.
- Утечек слушателей/таймеров в не-чатовом коде не найдено; стоимость — churn перерисовок.

### CSS

6 `backdrop-filter` (в том числе мобильная панель навигации поверх прокручиваемых списков и каждая `.panel-card`),
4 `radial-gradient` фона, 92 `box-shadow`, 37 `:has(` (цепочка `:has(> :nth-child(24)):not(:has(> :nth-child(25)))`
в аналитике инвалидируется на каждую мутацию поддерева с 15k узлов), 481 `!important`, 47 `@media`
(26 дублирующих блоков `max-width: 900px`), `content-visibility` не используется.

### Backend

| Место | Проблема |
| --- | --- |
| `services/analytics.py:145–294, 522–612` | 12 проходов по `messages` на один `/api/analytics/chats`, несаргируемый `date(datetime(created_at, ?))`, ≈50 `LOWER(raw_json) NOT LIKE` на строку; response-blocks без фильтра периода; drilldown дублирует 1000 строк в `hourly[].chats` (фронт не читает) |
| `repository.py:4026–4104` `list_tasks` | N+1 `_load_task_comments`, `ORDER BY julianday()` мимо индексов, DDL-проверка `_ensure_task_types_table` на каждый вызов, без LIMIT |
| `repository.py:4376–4399`, `4549–4572` | `reviews`/`ozon_questions` без единого индекса, `LIMIT 500`, сортировка по `datetime(COALESCE(...))`, полный `raw` в каждой строке; `unanswered` для вопросов фильтруется в Python после LIMIT |
| `repository.py:4137–4155` | `knowledge_articles` без индексов, полный `content` в списке, `LIKE` по `content` с первого символа |
| `repository.py:1995–2072` | `GET /api/notifications` делает две write-проходки (cleanup + stale-resolve) на каждый 10-секундный poll |
| `repository.py:4247–4298` `stats()` | Коррелированный подзапрос с `ORDER BY datetime()` на каждый незакрытый чат |
| `db.py:368–383` | Новое SQLite-соединение с 5 PRAGMA на каждый `get_connection()`, ≥2 на API-запрос |
| `server.py` | wsgiref HTTP/1.0 без keep-alive, поток на соединение (инфраструктурное решение владельца) |
| `app/static/icons/Gradient-Mesh-27.jpg` | 25 KB, нигде не используется |

## Сводка по влиянию (что имеет смысл исправлять в этой задаче)

| # | Исправление | Выигрыш | Риск | Общее с чатами |
| --- | --- | --- | --- | --- |
| 1 | `Cache-Control: public, max-age=31536000, immutable` для `/static/*` (HTML — `no-store`), `GZipMiddleware` | −80 % байт на вход, повторные входы из кэша; −9 запросов | низкий–средний (глобально; нужен тест заголовков) | **да — согласование** |
| 2 | Убрать дубль `loadNotifications` в `refreshTimer`; `refreshVisibleData` для аналитики без авто-перезапроса drilldown; `task-types`/`chat-settings` не перезапрашивать при входе | −10…15 запросов/мин | низкий | нет (таймеры чатов не трогаются) |
| 3 | Делегирование событий + пропуск рендера при неизменном ответе в списках задач/отзывов/вопросов/знаний/drilldown; drilldown постранично | −15k узлов/мин в аналитике, −5k/12 с в вопросах | низкий | нет |
| 4 | Self-host Onest (variable woff2), убрать 7 несуществующих весов; `theme-init.js` — оставить файл, но с кэшем | −2 внешних соединения, −1 render-blocking запрос | низкий (CSP уже допускает `'self'`) | да (шрифт общий) — согласование |
| 5 | Убрать `backdrop-filter`/mesh/лишние тени (совместно с дизайн-системой этапа 5) | плавная прокрутка на мобильных | низкий | да — scoped или согласование |
| 6 | Backend без смены контракта: один запрос комментариев в `list_tasks`, индексы `reviews(marketplace, published_at)`, `ozon_questions(published_at)`, `knowledge_articles(is_published, updated_at)`, `task_comments(task_id)`; UTC-границы в аналитике | кратное ускорение списков на больших таблицах | средний (нужны characterization-тесты равенства выдачи) | нет, но это backend — отдельное решение владельца |

Вне этой задачи (контракты API, таймеры чатов, `/api/notifications` write-проходки, рантайм wsgiref, retention
`audit_logs`) — перечислено в `audit/details/performance-full.md` как рекомендации.

## Замеры Navigation/Paint Timing на стенде

Заполняется из `audit/screenshots/before/before-report.json` после съёмки (поле `timing`: `domContentLoaded`,
`load`, `fcp`, `resources`, `transferBytes`, `domNodes`). Таблица «до/после» по разделам — в `audit/05-results.md`.
