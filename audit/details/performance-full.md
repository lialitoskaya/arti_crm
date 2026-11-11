# Аудит производительности Arti CRM (read-only)

База: рабочее дерево на `38b34e3` (`feat/ozon-fbo-supplies`). Раздел чатов вне области исправлений
(`audit/chat-exclusions.md`); общие механизмы помечены «общее с чатами». Цифры по размерам файлов —
фактические байты из `app/static/`; оценки строк/узлов DOM — расчётные, помечены «≈».

## 1. Загрузка

### 1.1 Критический путь первой отрисовки (`app/static/index.html`)

| Шаг | Где | Что блокирует | Размер |
| --- | --- | --- | --- |
| 1 | `index.html` (`main.py:2370–2371`, `FileResponse`) | HTML отдаётся с `Cache-Control: no-store` (`main.py:776–782`) — каждый вход качает заново | 86 988 B (gzip 16 649), сжатия нет |
| 2 | `<link rel="manifest">` `index.html:7`, `apple-touch-icon` `:12` | не блокируют | 664 B / 3 891 B |
| 3 | `preconnect` fonts.googleapis/gstatic `index.html:14–15` | подсказка, 2 внешних соединения | — |
| 4 | Google Fonts CSS `index.html:16` (`Onest:wght@400;500;600;700&display=swap`) | **render-blocking внешний CSS**: DNS+TLS к googleapis, затем 4 веса × 2 subset (cyrillic+latin) = ≈8 woff2 (≈100–150 KB) с gstatic | внешний |
| 5 | `<script src="/static/theme-init.js">` `index.html:17` | **синхронный блокирующий скрипт в `<head>`** — отдельный запрос (RTT) ради 501 B; тоже `no-store` | 501 B |
| 6 | `styles.css` `index.html:18` | render-blocking, без сжатия, `no-store` | 192 545 B (gzip 29 746) |
| 7 | `app.js` `index.html:1209` (конец body, без `defer`) | парсинг+выполнение 398 KB до `bootstrap()`; интерфейс закрыт экраном `authBootScreen` до `/api/auth/me` (`app.js:8245–8249`, `checkAuth` 7735) | 398 338 B (gzip 88 105) |
| 8 | 6 × `<img src="/static/icons/nav-*.svg" loading="eager">` `index.html:57–62` | 6 запросов по 745–1 175 B, тоже `no-store`, т.е. 6 дополнительных round-trip при каждом входе | 5 690 B |

Итого на один холодный вход **≈ 690 KB** несжатого статического трафика в 10 запросах
(`index.html` + `theme-init.js` + `styles.css` + `app.js` + 6 svg), и из-за `no-store` это повторяется при
каждом F5 / повторном открытии вкладки / открытии PWA. С gzip те же файлы ≈ 135 KB (−80 %).

Подтверждение дефектов кэширования:

- `main.py:512–530` `add_fastfox_cache_headers` ставит `public, max-age=3600` для `/static/`, но
  `main.py:776–782` `no_cache_frontend` объявлен позже и, как внешний middleware, перезаписывает заголовок на
  `no-store, no-cache, must-revalidate, max-age=0` для `/` и `/static/*`. Ветка `max-age=3600` — мёртвый код.
- Starlette `StaticFiles`/`FileResponse` выставляют `ETag` и `Last-Modified` и умеют отвечать 304, но при
  `no-store` браузер не хранит копию, поэтому условных запросов не бывает — файл качается целиком.
- `GZipMiddleware` в `app/main.py` отсутствует (grep `GZip` — 0 совпадений). Сжатия нет ни для статики, ни
  для JSON (`/api/reviews`, `/api/questions`, drilldown на 1000 строк — см. §5).
- `sw.js` (2 024 B) не кэширует ассеты: только `fetch` для `/api/auth/*` с `cache: 'no-store'` и push
  (`sw.js:11–15`); регистрируется с `updateViaCache: 'none'` (`app.js:818–820`).
- Версионирование уже есть (`?v=v96-...` в `index.html:17–18, 1209`), т.е. инфраструктура для
  `Cache-Control: public, max-age=31536000, immutable` на `/static/*` готова, HTML можно оставить `no-store`.
- Тесты пинят только `no-store` для `/health` при аварийном старте (`tests/test_runtime_startup.py:78`) и
  `private, max-age=3600` для чат-вложений (`tests/test_asset_proxy_security.py:336`); заголовки `/static/*`
  тестами не зафиксированы.

**Общее с чатами: да** — любое изменение `no_cache_frontend`/сжатия глобально (`audit/chat-exclusions.md` §4).

### 1.2 DOM `index.html` (все 11 разделов в документе сразу)

Подсчёт элементов HTML-парсером по `<main id=…>`:

| `main#…` | строка | элементов | inline `<svg>` |
| --- | --- | --- | --- |
| `ozonFboView` | 532 | 173 | 0 |
| `chatsView` | 122 | 141 | 2 |
| `analyticsView` | 318 | 91 | 1 |
| `techSettingsView` | 1056 | 68 | 0 |
| `supplyPlanningView` | 441 | 63 | 0 |
| `tasksView` | 816 | 62 | 2 |
| `usersView` | 972 | 59 | 0 |
| `knowledgeView` | 896 | 55 | 0 |
| `reviewsView` | 711 | 39 | 0 |
| `questionsView` | 764 | 38 | 0 |
| `profileView` | 1158 | 27 | 1 |
| вне `main` (shell, nav, мобильный лист «Ещё», login) | — | 310 | 28 |
| **всего** | | **1 126** (max глубина 14) | **34** |

Вывод: статический DOM небольшой (<1 500 узлов), 10 из 11 `main` скрыты классом `hidden`
(`showView` `app.js:7322–7327`). Стоимость DOM создаётся **динамическими списками** (§3), а не разметкой.
106 `<button>`, 68 `<input>`, 25 `<select>`, `<template>` не используется, inline `style=` нет.

Inline SVG: 34 вхождения, 17 уникальных, **17 дублей** (4 544 B суммарно): шеврон `M9 6l6 6-6 6` ×7,
иконка «Выйти» ×4, «Поставки» ×2, «FBO» ×2, «Уведомления» ×2, «Сотрудники» ×2 (`index.html:57–120`).
Это косметика (≈2 KB), а не узкое место; при желании — `<symbol>`/`<use>`.

### 1.3 Изображения и манифест

| Файл | Размер | Использование |
| --- | --- | --- |
| `icons/Gradient-Mesh-27.jpg` | 25 489 B | **не используется** — ни в `styles.css`, ни в `app.js`, ни в `index.html`, ни в `*.py` (единственное упоминание — `audit/00-overview.md`). Мёртвый ассет в репозитории; на трафик не влияет |
| `icons/app-icon-180.png` | 3 891 B | `apple-touch-icon` `index.html:12` |
| `icons/app-icon-192.png` | 4 391 B | `manifest.webmanifest`, иконка push (`sw.js:30–31`) |
| `icons/app-icon-512.png` | 14 842 B | `manifest.webmanifest` |
| `icons/nav-*.svg` ×6 | 745–1 175 B | `<img loading="eager">` `index.html:57–62` — 6 запросов, `no-store` |

`manifest.webmanifest`: оба PNG объявлены `purpose: "any maskable"` одним файлом (`manifest.webmanifest:12–24`)
— корректно по размеру, но maskable-вариант требует безопасной зоны; к производительности не относится.

## 2. Запросы по разделам

Источник: `showView` (`app.js:7283–7371`), `refreshVisibleData` (`3162–3205`), таймеры в `init()`
(`8947–8955`) и `startFrontendAutoSync` (`2756–2790`). Все вызовы `api()` идут с `cache: 'no-store'`
(`app.js:1377`), поэтому HTTP-кэш не участвует нигде.

### 2.1 Фоновые таймеры, живущие во всех разделах

| Таймер | Интервал | Что вызывает | Где | Общее с чатами |
| --- | --- | --- | --- | --- |
| `notificationsTimer` | 10 с (`NOTIFICATIONS_REFRESH_INTERVAL_MS` `app.js:2647`) | `GET /api/notifications?limit=30` (`1210`) | `8953–8955` | да (shell) |
| `alertsTimer` | 10 с (`ALERTS_REFRESH_INTERVAL_MS` `2646`) | **ещё раз** `loadNotifications()` + `GET /api/chats?…paginated=true` (30 чатов) когда `activeView !== 'chats'` | `2782–2789` | да (блок 2661–2841) |
| `frontendSyncTimer` | 5 с (`2639`) | `POST /api/sync/operator` (min gap 5 с) + `POST /api/questions/sync/ozon` (вне раздела «Вопросы» — `force:true`, gap 12 с `2642`) → затем `GET /api/questions` (500 строк) и при `count>0` `GET /api/stats` | `2761–2769`, `2710–2740` | да |
| `statusTimer` | 60 с | `GET /api/sync/status` (in-memory, дёшево `main.py:3870–3875`) | `8950–8952` | да |
| `refreshTimer` | 60 с | `refreshVisibleData()` — зависит от раздела (см. таблицу 2.2) + третий `loadNotifications()` (`3166`) | `8947–8949` | нет (ветки не-чатов) |
| `visibilitychange`/`focus` | по событию | `runFrontendSyncSoon` + `loadNotifications` + `runFrontendOzonQuestionsSync(force)` | `8924–8938` | да |

Итог для простаивающей вкладки в разделе «Задачи»/«Аналитика»: ≈12 `POST /api/sync/operator`/мин,
≈5 `POST /api/questions/sync/ozon`/мин (+5 `GET /api/questions` по 500 строк), ≈12 `GET /api/notifications`/мин
(два независимых 10-секундных таймера; дедупликация `notificationsLoadPromise` `1207` срабатывает только при
наложении), 6 `GET /api/chats`/мин, 1 `GET /api/sync/status`/мин, плюс раздел-специфичные запросы раз в 60 с.
**≈ 40 запросов/мин на одну вкладку без действий пользователя.** Каждый `POST` дополнительно пишет строку в
`audit_logs` (`main.py:356–372`, см. §5).

### 2.2 Запросы при входе в раздел и повторные запросы

| Раздел | Запросы при открытии (`showView`) | Повторные (таймеры/события) | Объём / лимиты | Проблема |
| --- | --- | --- | --- | --- |
| Аналитика | `Promise.all`: `GET /api/analytics/chats`, `GET /api/analytics/chats/drilldown?…&limit=1000&include_excluded=1` (`app.js:6360–6369`) | каждые 60 с `refreshVisibleData` → `loadAnalytics` + `loadStats` + `loadSyncStatus` (`3167–3171`); клик по часу → снова оба запроса (`6509–6514`) | drilldown: до 1000 `items` (~15 полей, `text_preview` 240 симв.) **+ те же строки продублированы в `hourly[].chats[]`** (`analytics.py:876–896`) → ≈0,5–1 MB JSON без сжатия; `excluded_service_items` до 300 | Нет кэша/дебаунса; отчёт целиком перезапрашивается и перерисовывается каждую минуту без изменения фильтров; `hourly[].chats` на фронте не читается (`renderAnalyticsDrilldown` использует только `items`, `excluded_service_items`, `filters` `6522–6530`) |
| Поставки (beta) | API нет: `readSupplyPlanningState()` из `localStorage` + сразу `writeSupplyPlanningState()` (`7023–7030`) | нет таймеров; `POST /api/supply-planning/sync` по кнопке (`6850–6863`) | все строки в `localStorage` (`SUPPLY_PLANNING_STORAGE_KEY` `261`) | Запись `localStorage` при каждом открытии и **при каждом вводе** (§3) |
| Поставки FBO | `GET /api/ozon-fbo/status` → при отсутствии активного прогона `GET /api/ozon-fbo/runs?limit=1`; параллельно `GET /api/ozon-fbo/runs?limit=30` (`9143–9180`, `9662`) | poll `GET /api/ozon-fbo/runs/{id}?after=seq` каждые 2 с (6 с в фоне) только при активном прогоне (`9394–9437`); останавливается в `showView` (`7304`) | инкрементальный лог по `after` | OK; 2–3 запроса на вход |
| Задачи | `GET /api/task-types?include_inactive=true` → `.finally` → `GET /api/tasks?…` (`7356–7358`) — последовательно | каждые 60 с `loadAllTasks` + `loadStats` + `loadSyncStatus` (`3173–3177`); поиск: debounce 220 мс, min 3 символа (`8360`, `5745`); `loadAssignees` при init/auth (`8900`, `7732`) | `/api/tasks` без LIMIT; каждая задача с полным массивом `comments` | `task-types` уже загружены в `init()` (`8267`) и перезапрашиваются при каждом входе; задачи без пагинации; N+1 на бэкенде (§5) |
| Отзывы | `GET /api/reviews?[status][unanswered]` (`4907–4915`) | каждые 60 с `loadReviews` + `openReview(current)` + `loadStats` + `loadSyncStatus` (`3183–3188`) | LIMIT 500, каждая строка с распарсенными `raw`, `media`, `comments` (`repository.py:4300–4309`) | Список тащит сырой payload Ozon по 500 отзывам; нет пагинации/индексов (§5) |
| Вопросы | `GET /api/questions` (`5263–5272`), через 400 мс `POST /api/questions/sync/ozon` (`runFrontendQuestionsSyncSoon` `2749–2754`) → снова `GET /api/questions` → `GET /api/stats` (`2723–2733`) | в разделе: `frontendSyncTimer` каждые 5 с с gap 12 с → POST sync + GET 500 вопросов + stats; каждые 60 с `refreshVisibleData` → то же (`3190–3193`) | LIMIT 500 + `raw` по каждому вопросу | 3–4 запроса на вход; **полная перезагрузка и перерисовка 500 вопросов каждые ≈12 с** |
| База знаний | `GET /api/knowledge/categories` → `GET /api/knowledge/articles` последовательно (`6154–6170`) | каждые 60 с `loadKnowledge` (оба запроса) (`3179–3181`); поиск debounce 250 мс без мин. длины (`8800`) → `LIKE` по `content` с 1 символа | статьи возвращаются **с полным `content`** (`SELECT ka.*` `repository.py:4150`), карточке нужно 120 символов (`6040`) | Payload пропорционален суммарному размеру всех статей; поиск по 1 символу |
| Сотрудники | `GET /api/users` (`7888`) | нет | малый | OK |
| Тех. настройки | `GET /api/chat-settings`, `GET /api/task-types`, `GET /api/admin/yandex-oauth-links` (`7363–7366`) | нет | малый | `chat-settings` и `task-types` уже загружены в `init()` (`8266–8267`); 3 запроса вместо 1 |
| Профиль | нет (`fillProfileForm`) | нет | — | OK |
| init (все разделы) | `/api/auth/me`, `/api/security/csrf`, `/api/chat-settings`, `/api/task-types`, `/api/users/assignees`, `/api/sync/status`, `/api/notifications`, push public-key/subscribe (`8245–8267`, `8900–8922`) | — | — | 7–9 запросов до первого полезного экрана, часть последовательна (`checkAuth` → `init`) |

### 2.3 Запросы в циклах / на строку

- В не-чатовом коде `await api()` внутри цикла — только `saveAllChatStatuses` (`app.js:2101–2103`: `for (const row of rows) await api(PATCH /api/chat-settings/statuses/{id})`) — последовательный PATCH на каждую строку настроек при сохранении (админ-действие, редкое).
- Per-row запросов при рендере списков нет. Проблема в другом: per-row **DOM-операции и слушатели** (§3).
- Бэкенд делает per-row запросы сам: `/api/tasks` → `_load_task_comments` на каждую задачу (§5).

## 3. Рендеринг и утечки

### 3.1 Полные `innerHTML`-перерисовки

| Функция | Где | Триггер | Оценка DOM | Детали |
| --- | --- | --- | --- | --- |
| `renderAllTasks` | `app.js:5760–5841` | каждый `loadAllTasks`: вход, каждые 60 с, каждое изменение фильтров, поиск (220 мс) | ≈35 элементов/карточка (2 inline SVG + скрытая панель редактирования `taskBoardEditPanelHtml` на **каждую** карточку) → 100 задач ≈ 3 500 узлов | `box.innerHTML = ''`, затем `createElement` + `innerHTML` на карточку и `bindTaskBoardCardActions` (`5626–…`): ≥5 `addEventListener` на карточку (пилюля статуса, каждая опция статуса, edit, cancel, delete, «открыть чат» `5826–5833`). Меню статусов и панель редактирования создаются заранее для всех карточек, хотя открыта максимум одна |
| `renderReviewsList` | `4917–4944` | вход, фильтры, каждые 60 с | ≈10 узлов/элемент × 500 = ≈5 000 | полная пересборка даже если данные не изменились |
| `renderQuestionsList` | `5275–5309` | вход, фильтры, **каждые ≈12 с** (sync-цикл `2723–2729`) и каждые 60 с | ≈10 узлов × 500 = ≈5 000 + `bindInlineQuestionForms()` | самый частый полный rebuild среди не-чатовых списков |
| `renderKnowledgeArticles` → `renderKnowledgeCategories` | `6172–6191`, `6066–6146` | вход, каждые 60 с, каждый тик поиска (250 мс), **`resize` окна** (120 мс, `8801–8810`) | категории × статьи (фильтрация `allArticles.filter` на каждую категорию `6077`, `6108`) | `querySelectorAll` + `onclick` переназначение; на desktop список рендерится дважды (в категориях и в `knowledgeArticlesList` скрывается `6187–6189`) |
| `renderAnalyticsDrilldown` | `6518–6605` | каждый `loadAnalytics` (вход, 60 с, клик по часу) | ≈12 узлов/строка × 1000 + ≈9 × 300 excluded ≈ **15 000 узлов** | один `tableEl.innerHTML` на 1000 `<tr>`, затем `querySelectorAll('[data-open-analytics-chat]')` и **1000 `addEventListener`** (`6577–6584`) вместо одного делегированного обработчика |
| `renderAnalyticsHourly` | `6488–6517` | тот же | 24 кнопки + 24 слушателя | мелочь |
| `renderSupplyPlanning` | `7004–7021` | открытие, смена фильтров (`7071–7073`), reset | все строки (`supplyPlanningRowHtml`) | ввод в поле не перерисовывает таблицу (точечное обновление `7032–7054`) — хорошо, но см. ниже |
| `handleSupplyPlanningInput` | `7055–7066` | **каждое событие `input`** | — | `writeSupplyPlanningState()` → `JSON.stringify` всех строк + синхронная запись `localStorage` (`6742–6751`) на каждый символ; при сотнях строк SKU×склад это блокирующая операция на каждый ввод |
| `renderNotifications` | `1145–1205` | каждый poll (10 с ×2) | ≤3 тоста | 3 слушателя на тост, пересоздаются каждые 10 с — незначительно |

Общее: ни один список не сравнивает новые данные со старыми (нет ключей/диффа/хэша ответа); любой
poll = полный rebuild. `escapeHtml` строковый (`7372–7379`), вызывается ≈6 раз на строку drilldown — не узкое место.

### 3.2 Debounce / throttle

- Есть: поиск задач 220 мс + min 3 символа (`8360`, `5745`); поиск базы знаний 250 мс (`8800`) **без минимальной длины**; `resize` для базы знаний 120 мс (`8803`); `chatSearchTimer` (чаты).
- Нет: фильтры отзывов/вопросов (`change` → немедленный запрос, нормально), клик по часу аналитики (два тяжёлых запроса на каждый клик без отмены предыдущих — `AbortController` используется только для таймаута `1366–1370`), переключение разделов (повторный вход = повторные запросы, предыдущие не отменяются).

### 3.3 Слушатели, наблюдатели, таймеры

- Утечек слушателей в не-чатовом коде не найдено: `init()` защищён `appInitialized` (`8257–8258`), `bind()` (`7628–7635`) вызывается один раз; `document`-слушатели на уровне модуля регистрируются однократно (`5843` закрытие меню статусов задач — top-level, не внутри `renderAllTasks`; `8811` Esc базы знаний; `9728` Esc FBO; `8533` pointerdown). `bindSupplyPlanningControls` защищён `dataset.boundSupplyPlanning` (`7085–7089`). Слушатели на удалённых через `innerHTML` узлах собираются GC. Стоимость — churn, а не утечка.
- Наблюдатели: `IntersectionObserver` (`3939–3947`) и `ResizeObserver` (`5912–5914`) — только чаты. В не-чатовых разделах ленивой подгрузки/виртуализации нет.
- `requestIdleCallback`: обёртка `runWhenBrowserIsIdle` (`3277–3283`) есть, не-чатовые рендеры её не используют.
- Таймеры при смене раздела: FBO poll останавливается (`7304`); глобальные 60/10-секундные таймеры по дизайну живут всегда; `window.__kbSearchTimer`/`__kbResizeTimer` висят на `window`, но защищены проверкой `activeView` (`8804`). Проблема не в незачищенных таймерах, а в том, что `refreshTimer` каждую минуту выполняет тяжёлый сценарий раздела (аналитика: 2 запроса + 15k узлов) даже при простое пользователя — ограничение только `document.hidden` (`3163`, `8948`).
- `matchMedia(...)` создаётся заново при каждом вызове `isMobileQuestionInlineLayout`/`isKnowledgeMobile` (`5173`, `5994`) — вызывается один раз на рендер, не на строку; незначительно.

## 4. CSS (`app/static/styles.css`, 6 484 строки, 192 545 B)

| Метрика | Значение | Строки / комментарий |
| --- | --- | --- |
| `backdrop-filter` / `filter: blur` | 6 | `649` (extra-панель чата), `2241` (мобильный nav), `3388–3389` `.app-nav` blur(16px) saturate, `3480` `.settings-card/.profile-card/.user-card-panel` blur(18px), `3594` мобильный `.app-nav` blur(18px) |
| `radial-gradient` | 4 | только фон `html[data-theme] body` `3351–3363` (2 radial + 1 linear на тему) |
| `linear-gradient` | 8 | `3212`, `3355`, `3362`, `3398`, `3404`, `3413`, `3435`, `3532` |
| `box-shadow` | 92 | 3 с blur ≥30px (`3387` 30px, `3391` 34px, `5987` 80px `!important`); токены тем `--crm-shadow-md: 0 22px 60px` / `0 26px 70px` (`3308`, `3347`, `3892`) применяются ко всем панелям |
| `:has(` | 37 | блок аналитики `1858–1949`: вложенные `:has(> .analytics-panel-head + *:has(> :nth-child(24)):not(:has(> :nth-child(25))))` — проверка «ровно 24 дочерних» на каждую мутацию поддерева `.analytics-page`, где drilldown меняет ≈15k узлов за раз; рядом те же правила уже заданы через `#peakHoursPanel` / `.peak-hours-panel` (`1859–1861`), т.е. `:has`-цепочка — избыточный fallback |
| `:nth-child` | 109 | в основном та же зона аналитики |
| `transition: all` | 0 | хорошо; 6 `transition:` всего |
| универсальные селекторы | `* { box-sizing }` `47`; `.analytics-page *, *::before, *::after { min-width: 0 }` `1817–1819` | второй — на всё поддерево аналитики |
| `will-change` | 3 | `2513`, `2518`, `2596` — чаты |
| `contain` / `content-visibility` | 1 / 0 | `2614` (чаты); для длинных списков не-чатов `content-visibility: auto` не используется |
| `@container` | 2 | чаты |
| `@media` | 47 | 26 отдельных блоков `max-width: 900px`, 6 × `760px`, 4 × `560px`, 3 × `1100px` — дублирование точек останова (стоимость сопровождения, не матчинга) |
| `!important` | 481 | слой-поверх-слоя (`5980–5988` панель с 8 `!important`); каскад переопределений вместо одной реализации |
| custom properties | 175 объявлений / 551 `var()` | 3 набора токенов: `:root` `4–47`, light/dark `3272–3349`, dark-слой `3864–3893` |
| `@font-face` | 0 | шрифт только через Google CSS |

Оценка стоимости repaint:

- `body { overflow: hidden }` (`49–51`), прокрутка идёт внутри панелей, фон `html[data-theme] body` статичен —
  градиент сам по себе на скролл не перерисовывается. Но панели полупрозрачные (`--crm-panel: rgba(255,255,255,.92)`
  `3276`, `--crm-nav: rgba(…,.64)` `3280`; `.app-shell/.workspace` прозрачные `3365`), поэтому композитор
  смешивает каждый прокручиваемый список с градиентом под ним.
- Desktop: blur на `.app-nav` (боковая панель, под ней ничего не скроллится) — одноразовая стоимость слоя.
- Mobile (`≤900px`): `.app-nav` становится нижней панелью с `backdrop-filter: blur(16–18px)` (`2236–2241`, `3592–3595`)
  **поверх прокручиваемого контента** → пере-размытие на каждом кадре прокрутки списков отзывов/вопросов/задач.
  Это самый дорогой CSS-эффект на слабых телефонах.
- `.settings-card/.profile-card/.user-card-panel` blur(18px) (`3474–3480`) — карточки внутри прокручиваемых
  разделов «Тех. настройки»/«Профиль»/«Сотрудники»: каждая — отдельный слой с blur.

## 5. Backend

Контекст исполнения: `pm2` → `server.py` → `wsgiref.simple_server` + `ThreadingMixIn` (`server.py:11–12`,
`53–61`) → `a2wsgi.ASGIMiddleware` (один event loop в фоновом потоке, `ASGIMiddleware.__init__`) → FastAPI.
58 маршрутов `def` (sync) и 32 `async def` (подсчёт по `app/main.py`). Sync-маршруты исполняются в
threadpool anyio (по умолчанию 40 потоков); под uvicorn — то же самое. Отличия wsgiref:
`WSGIRequestHandler` работает по HTTP/1.0 без keep-alive → **новое TCP-соединение на каждый запрос**
(≈40 запросов/мин на вкладку по §2.1 + 10 статических на вход), поток на соединение, нет sendfile — `FileResponse`
400 KB `app.js` прокачивается кусками 64 KB через ASGI→WSGI мост в Python.
`get_connection()` (`db.py:368–383`) открывает **новое соединение SQLite на каждый вызов** и выполняет 5 PRAGMA
(`busy_timeout=15000`, `foreign_keys`, `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`); пула нет,
`cache_size`/`mmap_size` не заданы. Каждый API-запрос открывает минимум 2 соединения: `get_user_by_session`
в middleware (`main.py:445–446` → `repository.py`) + обработчик. WAL + `BEGIN IMMEDIATE` в писателях
(`repository.py:936, 1487, 3252…`) — читатели не блокируются; но `busy_timeout=15000` означает, что ждущий
писатель держит рабочий поток (и поток соединения wsgiref) до 15 с.

| Endpoint | Запрос / код | Проблема | Индекс / исправление (без смены контракта) | Риск |
| --- | --- | --- | --- | --- |
| `GET /api/analytics/chats` | `build_chat_analytics` `analytics.py:907–1010`: 12 запросов (`_fetch_summary`, raw count, excluded count, response stats, closed count, outside, weekend, daily ×3, hourly, marketplace breakdown), каждый заново строит CTE `inbound_ranked` (`244–294`) | Фильтр `date(datetime(m.created_at, ?)) BETWEEN ? AND ?` (`283`) не sargable — индекс `idx_messages_direction_created_at` (`db.py:1047`) используется только по `direction`, диапазон дат вычисляется для каждой входящей строки; `_analytics_client_message_sql` (`145–218`) добавляет ≈50 `LOWER(raw_json) NOT LIKE '%…%'` на строку (`raw_json` — полный payload маркетплейса); `ROW_NUMBER() OVER (PARTITION BY chat_id, date(datetime(...)))` → сортировка. `_response_blocks_cte_sql` (`522–612`) делает window `SUM` по **всем** сообщениям без фильтра периода (`569–572`) и коррелированный подзапрос `MIN(o.created_at) … julianday(o.created_at) > julianday(b.request_at)` на каждый блок (`598–604`); затем Python-агрегация по каждому блоку (`613–645`). Итого ≈12 полных проходов по `messages` с 50 LIKE на строку за один вызов, а вызов идёт каждые 60 с на вкладку | В рамках SQL без изменения семантики: вычислять границы периода в UTC один раз в Python и фильтровать `m.created_at BETWEEN ?utc_start AND ?utc_end` (sargable, индекс по `created_at`), оставив `date(datetime(...))` только в SELECT; материализовать `inbound_ranked` один раз во временной таблице на соединение и читать из неё 12 раз; в response-blocks добавить фильтр периода в `ordered_messages`. Предвычисленный флаг «сервисное сообщение» — изменение схемы (отдельное решение) | средний: нужны characterization-тесты на равенство выдачи до/после; форматы `created_at` смешанные (ISO с offset и naive), поэтому UTC-границы требуют проверки `_parse_utc_datetime` (`75–93`) |
| `GET /api/analytics/chats/drilldown` | `analytics.py:709–906`, LIMIT 1000 (`784–785`), excluded LIMIT 300 (`844–845`) | Те же несаргируемые предикаты + `ORDER BY local_day DESC, local_hour DESC, datetime(created_at) DESC` после window-функции → полная сортировка; ответ дублирует каждую строку в `hourly[].chats[]` (`876–896`), UI это поле не читает → ×2 объём (≈0,5–1 MB) | Сжатие (глобально) снимает объём; `hourly[].chats` убрать нельзя без смены контракта — зафиксировать как кандидат; фронт: не перезапрашивать по таймеру, грузить по требованию | низкий (фронт) / средний (контракт) |
| `GET /api/stats` (`main.py:4561–4563`) | `repository.stats` `4247–4298`: коррелированный подзапрос на каждый незакрытый чат `ORDER BY datetime(m.created_at) DESC, m.id DESC LIMIT 1` (`4260–4271`); все неотвеченные вопросы читаются в Python и считаются циклом (`4280–4290`) | `datetime()` в ORDER BY не даёт использовать `idx_messages_chat_created_id` (`db.py:1051`) → сортировка сообщений каждого чата; вызывается из `loadStats` после каждой sync вопросов с `count>0`, после patch задач, каждые 60 с в аналитике/задачах/отзывах | `ORDER BY m.created_at DESC, m.id DESC` по индексу даёт тот же результат, если формат `created_at` однороден (проверить characterization-тестом); счёт вопросов — `COUNT(*)` с условием в SQL | низкий–средний; **общее с чатами: да** (`#stats` в сайдбаре чатов) |
| `GET /api/tasks` | `list_tasks` `repository.py:4026–4104` + `_decorate_task` `3849–3875` | **N+1**: `_load_task_comments` (`3836–3847`) на каждую задачу; `ORDER BY julianday(t.due_at) DESC` (`4098`) — индексы `idx_tasks_due_at_id`/`idx_tasks_assigned_due_id` (`db.py:1087–1090`) для сортировки не используются; поиск `LIKE` в 4 регистровых вариантах (`4061–4067`); `_ensure_task_types_table(conn)` (DDL-проверка) на каждый вызов (`4076`); без LIMIT; ответ содержит полный `comments[]` на задачу (доска показывает `getTaskPrimaryText`/`last_comment`) | Один запрос комментариев `WHERE task_id IN (…)` с группировкой в Python — выдача идентична; сортировка по `t.due_at` напрямую, если значения в ISO (проверить); `_ensure_task_types_table` вынести в `init_db` | низкий |
| `GET /api/reviews` | `list_reviews` `4376–4399`: `SELECT *` LIMIT 500, `ORDER BY datetime(COALESCE(published_at, updated_at, created_at)) DESC` | Нет ни одного индекса на `reviews` (список индексов `db.py:1047–1095`) → полный скан + сортировка по вычисляемому выражению; `_review_row_to_dict` (`4300–4309`) парсит и возвращает `raw` (весь payload Ozon), `media`, `comments` по каждому из 500 отзывов — список использует ~8 полей (`app.js:4922–4940`) | Индекс `reviews(marketplace, published_at DESC)` ускорит только при замене выражения сортировки (COALESCE остаётся несаргируемым); убрать `raw` из списка — смена контракта (кандидат на `fields=`/отдельный summary-эндпоинт, решение владельца); gzip снимает 5–10× объёма | низкий (индекс), средний (контракт) |
| `GET /api/questions` | `list_ozon_questions` `4549–4572`: то же + `raw`; фильтр `unanswered` частично применяется **в Python после LIMIT 500** (`4569–4571`) | Нет индексов на `ozon_questions`; вызывается каждые ≈12 с из sync-цикла (общее с чатами: `runFrontendOzonQuestionsSync` в блоке 2661–2841) | как для отзывов | как для отзывов |
| `GET /api/knowledge/articles` | `list_knowledge_articles` `4137–4155` | `SELECT ka.*` → полный `content` всех статей для карточки на 120 символов; `LIKE` по `title/content/tags` с любого символа; `ORDER BY datetime(ka.updated_at)`; индексов на `knowledge_articles` нет | Фронт: минимальная длина запроса; `ORDER BY ka.updated_at DESC` (ISO) по индексу `(is_published, updated_at)`; сокращение полей — контракт | низкий |
| `GET /api/notifications?limit=30` (`notifications_router.py:18`) | `list_notifications` `2040–2072` | На **каждый** poll (12/мин на вкладку): (1) `cleanup_read_marketplace_notifications()` — отдельное соединение, SELECT всех непрочитанных `new_message` JOIN chats, разбор `metadata_json` в Python, возможный UPDATE (`1995–2022`); (2) `_resolve_stale_message_notifications_conn` — UPDATE с EXISTS-подзапросами по `chats`/`messages` (`1236–…`) — попытка записи при каждом опросе даже без изменений; (3) SELECT с `datetime(created_at)>=datetime(?)` (`2024–2037`) — индекс `idx_notifications_user_read_created` (`db.py:1071`) работает только по `user_id`, диапазон и `ORDER BY datetime(n.created_at)` вычисляются | Границы дня передавать в формате хранимого `created_at` и сравнивать без `datetime()`; очистку (1)–(2) выполнять в фоновом цикле или по изменению (при импорте сообщений), а не в GET | средний; **общее с чатами: да** |
| `GET /api/users/assignees`, `GET /api/task-types`, `GET /api/chat-settings`, `GET /api/users`, `GET /api/sync/status` | `repository.py:4107–4116`, `3592–3610`, `4689–4692`; `main.py:3870–3875` | дешёвые (малые таблицы, `idx_task_types_active_sort`); `/api/sync/status` — из `app.state` | проблема только в частоте повторных вызовов с фронта (§2) | — |
| Все `POST/PATCH/DELETE /api/*` | `security_guard_middleware` → `_audit_action` `main.py:356–372` → `write_audit_log` (`repository.py`) | На каждый небезопасный запрос: `ensure_security_tables()` (executescript `CREATE TABLE/INDEX IF NOT EXISTS` ×4) + INSERT в `audit_logs`; retention нет (grep `DELETE FROM audit_logs` — 0). Sync-цикл фронта даёт ≈17 POST/мин на вкладку → ≈25k строк/день на вкладку | DDL-проверку выполнять один раз при старте; фоновые sync-POST можно исключить из аудита только решением владельца (безопасность) | низкий (DDL), политика (аудит); **общее с чатами: да** |
| `GET /static/*` | `KnowledgeSafeStaticFiles.lookup_path` `main.py:94–110` | На каждый статический запрос: 2× `os.path.abspath`, `super().lookup_path` (stat), затем `resolved_path_is_within` → 2× `os.path.realpath` (`knowledge_images.py:90–94`) — доли мс, но ×10 файлов × каждый вход, потому что `no-store` | устраняется кэшированием на клиенте | — |
| `GET /` | `FileResponse(index.html)` `main.py:2370–2371` | `no-store` оправдан для HTML (чтобы `?v=` подхватывался), но без gzip 87 KB | gzip | — |

Связанные наблюдения по БД (`app/db.py`): индексы есть для `messages`, `chats`, `tasks`, `notifications`,
`push_*`, `task_types`, `reply_templates`, `ozon_fbo_runs` (`1047–1095`, `982–984`); **нет ни одного** для
`reviews`, `ozon_questions`, `knowledge_articles`, `knowledge_categories`, `task_comments(task_id)`.
`sessions.session_token` объявлен `UNIQUE` (`db.py:529`) — неявный индекс есть, проверка сессии в middleware
по индексу. Подтверждено также, что `hourly[].chats` из drilldown фронтом не читается: `data.hourly` используется
только в `renderAnalytics` для ответа `/api/analytics/chats` (`app.js:6483`).

## 6. Шрифты и изображения

- Google Fonts URL (`index.html:16`): `Onest:wght@400;500;600;700&display=swap` — `display=swap` присутствует,
  FOUT вместо блокировки текста, но сам CSS-запрос render-blocking и идёт к третьей стороне (2 соединения,
  DNS/TLS; в РФ доступность googleapis нестабильна → `preconnect` не спасает от таймаута до swap).
- Фактически используемые веса в `styles.css`: `700` ×11, `600` ×8, `500` ×7, `400` ×6 и **не загружаемые**
  `650` ×3, `550`, `750`, `760`, `800` ×1 (grep `font-weight:`; `styles.css` — 7 объявлений). Браузер
  подставляет ближайший доступный вес (550→600, 650→700, 750/760/800→700), т.е. эти 7 объявлений
  не дают визуального эффекта и вводят в заблуждение; при переходе на variable-шрифт они внезапно «сработают».
- `@font-face` в проекте нет; `font-family` объявлена 4 раза (`44`, `1365`, `1757`, `6398`).
- Самостоятельный хостинг возможен **без изменения CSP**: `font-src 'self' data: https://fonts.gstatic.com`
  (`main.py:486`) уже содержит `'self'`; `style-src` включает `fonts.googleapis.com` (`485`) — после переноса
  оба внешних источника можно удалить из CSP (глобальное изменение, согласование). Onest распространяется по
  OFL; один variable woff2 (cyrillic+latin) ≈ 60–90 KB вместо ≈8 статических файлов, плюс `<link rel="preload" as="font">`
  и `font-display: swap` в `@font-face`. Файлы шрифтов получат кэш только после исправления `no-store` (§1).
- `theme-init.js` как отдельный блокирующий запрос: инлайн запрещён `script-src 'self'` без nonce/hash
  (`main.py:476–478`); вариант — добавить `'sha256-…'` хэш в CSP (глобально) либо оставить файл, но с кэшем.
- Изображения: PNG-иконки малы (3,9–14,8 KB), nav-SVG 0,7–1,2 KB; единственная проблема — `Gradient-Mesh-27.jpg`
  (25 KB) не используется нигде. Пользовательские изображения (вложения чатов, картинки базы знаний) отдаются
  через `FileResponse` с `private, max-age=86400` (`main.py:523–526`) — корректно.

## 7. Сводка — top-10 по влиянию

| # | Находка | Доказательство | Оценка выигрыша | Риск | Общее с чатами |
| --- | --- | --- | --- | --- | --- |
| 1 | **Статика без кэша и без сжатия**: `no_cache_frontend` перекрывает `max-age=3600`; `GZipMiddleware` отсутствует | `main.py:776–782`, `512–530`; размеры `app.js` 398 338 / `styles.css` 192 545 / `index.html` 86 988 B | ≈690 KB → ≈135 KB на вход (gzip), повторные входы — из кэша по `?v=` (0 KB JS/CSS); −9 запросов на вход; снимает нагрузку с wsgiref-потоков | низкий–средний: HTML оставить `no-store`, `/static/*` — `public, max-age` + immutable по `?v=`; нужен тест заголовков; изменение глобальное | **да** |
| 2 | **Фоновый fan-out ≈40 запросов/мин на простаивающую вкладку вне чатов**: два 10-секундных таймера на `/api/notifications`, `alertsTimer` тянет `/api/chats`, 5-секундный `POST /api/sync/operator`, `POST /api/questions/sync/ozon` + 500 вопросов + `/api/stats` | `app.js:2761–2789`, `8947–8955`, `3166`; каждый POST пишет `audit_logs` (`main.py:356–372`) | −50…70 % запросов/мин, −2 SQLite-соединения и −1 INSERT на каждый снятый запрос; на shared-хостинге это основной источник конкуренции за потоки | средний: таймеры в блоке 2661–2841 исключены — менять можно только `notificationsTimer`/`refreshTimer` (не-чатовые) без согласования, остальное — решение владельца | **да** |
| 3 | **Аналитика: 2 тяжёлых запроса на вход и каждые 60 с**; 12 проходов по `messages` с ≈50 `LIKE` по `raw_json` на строку, несаргируемые `date(datetime())`; drilldown 1000 строк ×2 в JSON (≈0,5–1 MB) и ≈15k DOM-узлов + 1000 слушателей на перерисовку | `analytics.py:145–218`, `244–294`, `522–612`, `876–896`; `app.js:6360–6369`, `3167–3171`, `6518–6605` | Фронт: убрать авто-refresh и грузить drilldown по требованию → −2 запроса/мин и −15k узлов/мин; бэкенд: UTC-границы + временная таблица CTE → оценочно 5–10× по CPU | фронт низкий; бэкенд средний (characterization-тесты на равенство ответа) | нет (данные чатов, но раздел — аналитика) |
| 4 | **Отзывы/вопросы: 500 строк с сырым payload маркетплейса, без индексов, сортировка по `datetime(COALESCE())`; полный rebuild 500 карточек каждые ≈12 с в «Вопросах»** | `repository.py:4376–4399`, `4549–4572`, `4300–4309`, `4450–4455`; `db.py:1047–1095` (нет индексов); `app.js:2723–2729`, `5275–5309` | gzip даст 5–10× по объёму сразу; пропуск рендера при неизменном ответе (сравнение по `id+updated_at`) уберёт ≈5 000 узлов каждые 12 с; сокращение полей — контракт | низкий (фронт/индексы), средний (поля ответа) | частично: `/api/questions` дергает общий sync-цикл |
| 5 | **Задачи: N+1 по комментариям, `julianday()` в ORDER BY, DDL-проверка на каждый вызов, `task-types` перезапрашиваются при каждом входе, 60-секундный полный rebuild карточек с предсозданными меню/панелями** | `repository.py:3836–3847`, `3849–3875`, `4076`, `4098`; `app.js:7356–7358`, `8267`, `5760–5841` | один запрос комментариев вместо N; −1 запрос на вход; меньше DOM на карточку (меню/панель создавать по клику) | низкий | нет (общие хелперы `patchTask` и т.п. не трогаются) |
| 6 | **`/api/notifications` выполняет две write-проходки (cleanup + stale-resolve) и несаргируемый SELECT на каждый 10-секундный poll** | `repository.py:1995–2022`, `1236–…`, `2024–2072` | убрать попытки записи из GET → меньше RESERVED-блокировок при нескольких операторах; индексный диапазон вместо `datetime()` | средний (поведение «прочитано» должно сохраниться — перенести в фоновый цикл/импорт) | **да** |
| 7 | **Шрифты: render-blocking Google CSS + ≈8 woff2 с третьей стороны; 7 объявлений несуществующих весов (550/650/750/760/800); `theme-init.js` — отдельный блокирующий запрос** | `index.html:16–17`; `styles.css` grep `font-weight`; `main.py:476–486` (CSP уже содержит `font-src 'self'`) | self-host variable Onest: −2 внешних соединения, −1 render-blocking запрос, кэшируемый шрифт; чистка весов — ноль риска | низкий; удаление googleapis из CSP — глобально | да (шрифт общий) |
| 8 | **База знаний: список отдаёт полный `content` всех статей ради 120-символьного превью; `LIKE` по `content` с первого символа; перерисовка категорий+карточек на каждый тик поиска и на `resize`** | `repository.py:4137–4155`; `app.js:6040`, `8800–8810`, `6066–6146` | min-длина запроса 2–3 символа и перерисовка на resize только при смене брейкпоинта — просто; сокращение полей — контракт | низкий | нет |
| 9 | **CSS: 481 `!important`, 26 дублирующих блоков `@media (max-width: 900px)`, вложенные `:has(…:nth-child(24))` в аналитике (инвалидация на каждую мутацию 15k узлов), `backdrop-filter` на мобильной нижней панели поверх прокручиваемых списков** | `styles.css:1858–1949`, `2236–2241`, `3592–3595`, `3474–3480`; счётчики §4 | заменить `:has`-цепочку на уже существующий `#peakHoursPanel`; убрать blur на мобильном nav → плавная прокрутка списков на телефонах; дедупликация медиа-блоков — сопровождение | низкий; токены и базовые селекторы общие с чатами — менять только scoped (`main.view`) | частично (`.app-nav`, токены — общие) |
| 10 | **Рантайм хостинга: wsgiref HTTP/1.0 без keep-alive + поток на соединение + один ASGI-loop; новое SQLite-соединение с 5 PRAGMA на каждый вызов `get_connection()` (≥2 на API-запрос); `audit_logs` без retention; мёртвый ассет `Gradient-Mesh-27.jpg` 25 KB и 17 дублей inline SVG** | `server.py:11–12, 53–61`; `db.py:368–383`; `main.py:445–446`; `repository.py` `ensure_security_tables`; `app/static/icons/` | uvicorn под pm2 вместо wsgiref даёт keep-alive и sendfile (инфра-решение владельца); переиспользование соединения в пределах запроса — архитектурное изменение; удаление ассета — ноль риска | инфра: средний; остальное низкий | **да** |

Вне топ-10, но зафиксировано: `renderAnalyticsDrilldown`/`renderAllTasks` вешают слушатель на каждую строку
вместо делегирования (§3.1); `handleSupplyPlanningInput` пишет весь `localStorage`-снимок на каждый символ
(§3.1); `saveAllChatStatuses` делает последовательные PATCH в цикле (§2.3); `/api/questions?unanswered`
фильтрует в Python после LIMIT 500 (§5); статический DOM из 11 разделов (1 126 узлов) сам по себе проблемой
не является (§1.2).

Предложения, затрагивающие заголовки кэша, сжатие, CSP, таймеры из блока `2661–2841`, `/api/notifications`,
`/api/stats` и рантайм сервера — **глобальные** и требуют отдельного согласования владельцем
(`audit/chat-exclusions.md` §4). Предложения по API помечены «контракт» и в план без решения владельца не входят.
