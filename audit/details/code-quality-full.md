# Этап 3. Качество кода фронтенда (read-only аудит)

База: рабочее дерево на `38b34e3` (`feat/ozon-fbo-supplies`). Файлы: `app/static/index.html` (1211 строк, 85 780 байт),
`app/static/app.js` (9731 строка, 398 338 байт, 484 функции верхнего уровня), `app/static/styles.css` (6484 строки,
1092 правила, 3612 деклараций), `sw.js`, `theme-init.js`, `manifest.webmanifest`. Измерения сняты скриптами
`css_audit.py`, `html_audit.py`, `js_audit.js` (в этой же папке; результаты в `css2.json`, `html.json`, `js.json`).
Парсинг JS — собственный сканер (без acorn), поэтому цифры по JS точны до ±1–2 на эвристиках (регулярные
литералы, стрелочные функции с телом-выражением).

Пометки: **«чаты — только информационно»** — находка внутри исключённых областей (`audit/chat-exclusions.md`), правки
запрещены; **«общее с чатами»** — селектор/функция/токен используется и чатами, менять можно только через отдельный
вариант для остальных разделов.

---

## CSS

### C1. Структура файла: слои поверх слоёв

| Слой (заголовок в файле) | Строки | Правил `{` | `!important` | Селекторов `html[data-theme` | `:has(` |
| --- | --- | --- | --- | --- | --- |
| База «clean light redesign» | 1–1357 | 359 | 15 | 5 | 0 |
| Typography / chat-list fixes | 1358–1801 | 63 | 0 | 0 | 0 |
| «targeted UI patch v11» (аналитика, тех.настройки, mobile chat) | 1802–2465 | 90 | **79** | 0 | **79** |
| v16–v47 runtime-слои (чаты) | 2466–3269 | 128 | 23 | 0 | 0 |
| v52 light/dark skin | 3270–3599 | 48 | 0 | 117 | 0 |
| v54 chat theme (чаты) | 3600–3863 | 38 | 78 | 68 | 0 |
| v55 dark hardening | 3864–4180 | 22 | 43 | 26 | 0 |
| v56 dark buttons | 4181–4385 | 16 | 39 | 37 | 0 |
| v57 questions/analytics dark | 4386–4547 | 27 | 16 | 13 | 0 |
| v58 chat status settings | 4548–4817 | 41 | 13 | 7 | 0 |
| v59 analytics chart | 4818–4949 | 12 | **76** | 10 | 0 |
| v60 tasks | 4950–5100 | 24 | 2 | 1 | 0 |
| v83 knowledge | 5101–5260 | 24 | 0 | 23 | 0 |
| v64–v67 supply | 5261–5641 | 70 | 6 | 13 | 0 |
| v71–v74 tasks/analytics | 5642–5868 | 43 | 13 | 2 | 0 |
| v81 mobile cleanups | 5869–6095 | 36 | 78 | 8 | 0 |
| v84 knowledge cards | 6096–6257 | 21 | 0 | 0 | 0 |
| v95.2 date filters | 6258–6365 | 17 | 0 | 0 | 0 |
| FBO | 6366–6484 | 64 | 0 | 0 | 0 |

76 комментариев-заголовков уровня колонки 0; 47 блоков `@media`, 2 `@container`, 1 `@keyframes`.

### C2. Селекторы, определённые ≥ 3 раз

Точных повторов (один селектор, один медиа-контекст) — 18; с учётом разных `@media` — 60. Топ-40 (строки):

| Селектор | Раз | Строки (медиа) | Пометка |
| --- | --- | --- | --- |
| `:root` | 6 | 4, 1265 (≤1280), 1342 (≤560), 1764 (≥901), 3272, 5103 | общее с чатами (токены) |
| `body` | 6 | 48, 49, 1289 (≤900), 1359, 1368, 2213 (≤900) | общее |
| `button` | 6 | 60, 61, 62, 69, 1359, 1374 | общее |
| `select` / `textarea` | 6 | 60, 61, 156, 167/168, 1359, 1374 | общее |
| `input` | 5 | 60, 61, 156, 1359, 1374 | общее |
| `.chat-header` | 6 | 524, 576, 604, 1316, 1741, 2989 | чаты — информационно |
| `.supply-planning-filters` | 6 | 5546, 5603 (≤1100), 5613 (≤760), 5622, 5631 (≤1280), 5637 (≤760) | поставки |
| `#tasksView .tasks-ref-toolbar` | 6 | 5658, 5693 (≤900), 5704, 5811 (≤1100), 5826 (≤760), 6361 (1101–1250) | задачи |
| `.workspace` | 5 | 325, 1307, 1343, 2213, 2271 | общее |
| `.chat-controls` | 5 | 560, 587, 605, 1317, 2995 | чаты |
| `.messages` / `.message` | 5 / 5 | 663…2819 / 673…2823 | чаты |
| `html[data-theme="dark"]` (блок токенов) | 5 | 3312, 3869, 3895, 4185, 5120 | общее — **5 отдельных блоков dark-токенов** |
| `.app-shell` / `.app-nav` / `.nav-btn` / `.layout` | 4 | 210/220/259/340 + 1271…2277 | общее (shell) |
| `.panel-card` | 4 | 334, 1344 (≤560), 1741, 2277 (≤900) | общее |
| `.tech-settings-page` | 4 | 1145, 1285 (≤1080), 2089, 2459 (≤560) | тех. настройки |
| `#chatsView .chat-badges` | 4 | 1594, 1603, 1617, 1775 | чаты |
| `.message.message-has-images .image-card` | 4 | 2862, 2894, 2910, 2926 | чаты |
| `#tasksView .tasks-ref-list` | 4 | 4951, 5077 (≤900), 5664, 5697 (≤900) | задачи |
| `html` | 3 | 48, 1359, 2213 | общее |
| `.mobile-back-btn`, `#extraMenuBtn`, `.main-nav`, `.account-nav`, `.notifications-widget`, `.sidebar`, `.conversation`, `#chatSubtitle`, `.chat-controls select`, `#chatStatus`, `#assignedUserSelect`, `.extra-panel`, `.composer`, `.composer-send-btn` | 3 | см. `css2.json:dup_selectors_any_media` | чаты / shell |
| `.review-item` | 3 | 405, 1347 (≤560), 1741 | отзывы |
| `.analytics-filters` / `.analytics-grid` | 3 | 974/982, 1266–1267 (≤1280), 1322 (≤900) | аналитика |
| `#tasksView .tasks-ref-card` / `.tasks-ref-card` | 3 / 3 | 1001, 4958, 5670 / 1053, 1322, 1741 | задачи |
| `.settings-grid` | 3 | 1145, 2089, 2459 | тех. настройки |
| `.tech-settings-page .chat-status-create-form` | 3 | 2116, 4549, 4780 (≤900) | тех. настройки |
| `.tech-settings-page .task-type-settings-row select` | 3 | 2151, 2159, 2170 (подряд, один блок) | тех. настройки |
| `#tasksView .tasks-ref-status-menu` | 3 | 4974, 5090 (≤900), 5679 | задачи |
| `.supply-planning-summary.analytics-grid`, `.supply-planning-row-fields` | 3 | 5315/5346, 5466–5470 (≤1100), 5480 (≤760) | поставки |
| `#tasksView .tasks-ref-title-row`, `#tasksView .tasks-standalone-grid` | 3 | 5708/5770, 5815–5820 (≤1100), 5830–5838 (≤760) | задачи |

**Одинаковые пары селектор+свойство, объявленные повторно** (тот же селектор, то же медиа, то же свойство): 177 пар,
178 «перекрытых» деклараций; 72 пары — селекторы чатов. Вне чатов лидируют:

| Селектор | Перекрытых свойств | Где |
| --- | --- | --- |
| `:root` | **31** | база 5–42 → v52 3274–3309 (`--crm-bg … --crm-focus`): v52 объявлен как `:root, html[data-theme="light"]`, поэтому базовый блок токенов 4–47 для этих 31 имён **мёртв** (всегда перекрыт). При этом `--crm-bg: #ffffff0000` (3274) — 10 hex-символов, невалидное значение: браузер отбрасывает декларацию, и `--crm-bg` остаётся `#f3f3f3` из базы — скрытая зависимость от «мёртвого» блока |
| `html[data-theme="dark"]` | 22 | токены объявляются в 5 блоках: 3312, 3869, 3895, 4185 (`--crm-button-*`), 5120 (`--knowledge-*`) |
| `.app-nav` @≤900px | 7 | 1291 → 2222 |
| `.notification-close-btn` | 5 | один блок уведомлений |
| `html[data-theme="dark"] button:disabled` / `.button-like:disabled` | 4 / 4 | 4357–4372 |
| `body` (`font-size` 14px→13px, `line-height`) | 2 | 54–55 → 1369–1370 |
| `.tasks-toolbar h2`, `.notification-toast-icon`, `.main-nav/.account-nav/.notifications-widget` @≤900, `.workspace` @≤900 | 2 | — |

Большинство «цепочек override» в файле идёт не через повтор того же селектора, а через наращивание специфичности
(`button` → `html[data-theme] button.secondary` → `html[data-theme="dark"] button` → `#tasksView .tasks-ref-new-btn … !important`),
поэтому их реальный масштаб измеряется `!important` и theme-префиксами ниже.

### C3. `!important` — 481

177 из 481 находятся внутри правил с префиксом `html[data-theme…]` (борьба скина со скином).

По блокам (заголовок → количество): v59 analytics chart (4818) **76**; mobile fullscreen chat (2202) 43 — чаты;
light theme fallback (3605) 40 — чаты; dark theme (3752) 38 — чаты; «peak hours thin-bar chart» (1857) **35**;
mobile knowledge (5947) **31**; mobile supply (6035) **21**; v57 analytics rows dark (4390) 16; база (1) 15;
internal notes (5872) 15 — чаты; v58 chat status settings (4548) 13; mobile analytics (5916) 11; v22 mobile (2531) 11 — чаты;
v56 buttons (4039) 10; v71 tasks scroll (5642) 10; остальные ≤ 8.

По свойствам: `background` 91, `color` 62, `border-color` 51, `box-shadow` 28, `display` 23, `overflow` 16, `gap` 14,
`width`/`height`/`padding`/`border`/`max-width` по 13, `min-height` 10, `grid-template-columns` 10, `min-width`/`overflow-x`/`z-index` 8,
`position`/`border-radius`/`transform` 7, `font-size` 6. Т.е. 232 из 481 (48 %) — цвета/фон/граница: это следствие
dark-скина через `html[data-theme="dark"] *`-правила, которые потом приходится перебивать в разделах.

Вне чатов и без учёта shared-слоёв: аналитика 76+35+16+11 = **138**, знания 31, поставки 21+6, задачи 10+13+2, тех.настройки 13.

### C4. `html[data-theme]`

330 вхождений префикса (брифом названо 315 — считали строки), 179 правил содержат префикс, 155 правил начинаются с него. Из 155:

- **47** объявляют только те свойства, которые уже есть в базовом правиле того же селектора (чистый override), из них
  **10 с теми же значениями** — шум без эффекта: 3457 (`.refresh-btn:hover …`), 3546 (`::placeholder`), 3561 (`.preview, …`),
  3587 (`::-webkit-scrollbar`), 4173 (`::-webkit-scrollbar-track`), 4538 (`#questionsView .question-inline-body p`),
  4922 (`#analyticsHourlyChart` — 6 свойств), 4944, 5453 (`.supply-planning-row-result`), 5799 (`#tasksView .tasks-ref-new-btn`);
- 53 частично перекрывают базу; 50 вводят селекторы, которых в базе нет (новые поверхности только для темы — например
  `html[data-theme] .app-nav`, `html[data-theme] .brand-card`, `html[data-theme] .panel-card, .sidebar, .conversation …` 3466).

Dark-скин v55 (3864–4180) и v56 (4181–4385) написаны «catch-all» (`html[data-theme="dark"] button`, `… input, select, textarea`,
`… .panel-card`), поэтому каждый новый раздел (v59, v81, FBO) добавляет свои `html[data-theme="dark"] #section …` исправления.

### C5. Магические значения

**Цвета вне токен-блоков** (`:root` 4–47 и 3272–3349 исключены): 305 вхождений, **199 различных литералов**. Топ-30 с ближайшим токеном:

| Литерал | Раз | Ближайший токен (Δ RGB) | Примеры строк |
| --- | --- | --- | --- |
| `#fff` | 41 | `--crm-panel` #ffffff (0) | 80, 96, 129, 162, 191, 228 … |
| `#ffffff` | 12 | `--crm-panel` (0) | 1473, 1478, 3352, 3619, 3639, 3688 |
| `#172033` | 8 | dark `--crm-card-hover` (4) | 3620, 3640, 3667, 3689, 3705, 3716 (чаты) |
| `#edf3ff` | 7 | dark `--crm-text` (0) | 3763, 3777, 3814, 3826 … (чаты) |
| `rgba(0,0,0,.28)` | 4 | — (тень) | 1214, 1239, 3391, 3890 |
| `#253448` | 4 | dark `--crm-panel-muted` (22) | 1420, 2632, 3762, 3825 |
| `rgba(0,0,0,.06)` / `rgba(255,255,255,.10)` | 3 / 3 | — | 1474, 1489, 2738 / 3771, 3785, 3855 |
| `#e5e9f1` | 3 | `--crm-primary-soft` (10) | 3641, 3690, 3717 |
| `#182234` | 3 | dark `--crm-card-hover` (6) | 4026, 4061, 4186 |
| `#22314a` | 3 | dark `--crm-panel-muted` (21) | 4087, 4187, 5128 |
| `#20354a`, `rgba(0,0,0,.16)`, `rgba(255,255,255,.08)`, `#dfeafa` (= `--crm-blue` Δ1), `#eeeeee` (= `--crm-primary-soft` Δ2), `#1f1f1f` (= `--crm-text` Δ5), `rgba(0,102,255,.10)`, `#eef5ff`, `#0a0f1b`, `rgba(148,163,184,.16)` (= dark `--crm-line` Δ1), `rgba(52,112,255,.06)`, `#e1e6ef`, `#eef2f7`, `#a8b3c2`, `#1b2738` (dark `--crm-panel-muted` Δ3), `#0b0f17` (= dark `--crm-bg`), `#121a29` (dark `--crm-bg-soft` Δ3), `#1a263a`, `#738198`, `rgba(99,162,255,.36)` | по 2 | см. колонку | `css2.json:top_colors` |

Остальные 160 литералов встречаются по одному разу (в т.ч. 5 вариантов «dark line» `rgba(126,148,178,.14/.16)`,
`rgba(148,163,184,.14/.16/.24)` и 4 оттенка «тёмной панели» `#172033/#182234/#1b2738/#1a263a`).

**`border-radius`**: токены `--crm-radius-{xs 5, sm 7, md 10, lg 14}` используются 1/12/13/11 раз; `999px` — 14 (pill);
литералы px — 28 деклараций, 13 различных значений (`12px`×6, `6px`×4, `4px`×3, `10px`×3, `7px`×2, `16px`×2, `18px`×2,
`3/5/8/11/13/14px` по 1); 7 из них равны существующему токену, но записаны числом (`10px` 3412/4602/4692, `7px` 845/4995,
`14px` 3446, `5px` 1638). Плюс 9 вариантов `var(--crm-chat-radius, …)` с разными фолбэками (чаты).

**`box-shadow`**: 21 различное значение; `none` 39, `--crm-shadow-xs` 15, `-md` 11, `-sm` 3, `--crm-focus` 3; **14 одноразовых
литералов** (`0 4px 12px rgba(0,0,0,.06)`, `0 7px 18px rgba(0,0,0,.16)`, `0 24px 80px rgba(15,23,42,.28)`, `0 10px 22px rgba(68,118,255,.24)` …).

**`font-size`**: 113 деклараций в px, **12 различных размеров** (`12px` 39, `13px` 26, `11px` 11, `14px` 8, `10px` 7, `15px` 7,
`16px` 6, `18px` 5, `20/22/24px`, `10.5px` 1687); 58 деклараций ≤ 12px. Токенов шрифтовой шкалы нет вообще; `body`
объявлен 14px/1.35 (54) и тут же перекрыт 13px/1.32 (1369).

### C6. Мёртвый CSS

Классы из `styles.css`, не встречающиеся ни в `index.html`, ни в `app.js` (точное слово, включая шаблонные строки):
**32 «likely dead»**, 36 «dynamic» (собираются как `status-${…}`, `ozon-fbo-state-${…}`, `supply-planning-status-${…}` — живые).

| Класс (`likely dead`) | Строки CSS | Комментарий |
| --- | --- | --- |
| `.analytics-peak-hours`, `.peak-hours-panel`, `.peak-hours-grid`, `.peak-hours-list`, `.analytics-table-grid`, `#peakHoursPanel` | 1857–1990 (37/34/96/96/99 вхождений) | **Целый блок 1857–2087 + его mobile-продолжение 2432–2458**: 34 правила, 106 `:nth-child`, все 37 `:has()` файла, ~40 `!important`. Это «псевдо-бары» часов из патча v11, рассчитанные на контейнер ровно с 24 детьми (`.analytics-panel:has(> .analytics-panel-head + *:has(> :nth-child(24)):not(:has(> :nth-child(25))))`). Слой v59 (4818) явно их гасит: `#analyticsHourlyChart … ::before/::after { content: none !important; display: none !important }` (4861–4866). Итог: ~260 строк дорогого селектора (`:has()` проверяется на каждой `.analytics-panel` при любом изменении DOM аналитики), результат которого перечёркнут |
| `.task-card-header/-main/-meta/-actions/-pill`, `.task-edit-actions` | 442, 1001, 1098–1103 | старая карточка задачи до `tasks-ref-*` |
| `.knowledge-tag` | 1139 | заменён `.knowledge-card-tags span` |
| `.analytics-daily-table` | 4127 | в dark-скине; в разметке `analytics-table` |
| `.supply-planning-delete` | 5439 | кнопки удаления строки нет |
| `.toast`, `.modal-card`, `.image-lightbox-card`, `.reply-template-menu`, `.notifications-panel`, `.profile-card`, `.user-card-panel`, `.knowledge-card`, `.knowledge-item`, `.task-item`, `.review-detail-card`, `.empty-illustration` | 3466, 3482, 3495, 3509, 3528, 3901 | перечислены в списках селекторов v52/v55 «на всякий случай» — таких элементов нет (общее с чатами: списки содержат и чат-классы, удалять только несуществующие имена) |
| `.chat-body`, `.composer-input`, `.chat-list-render-limit-note`, `.chat-list-load-more-note`, `.OZON`, `.ym` | 2606, 3168, 3523, 3536… | чаты — только информационно |

Ссылка на удалённый файл: заголовок 2202 `/* Mobile: fullscreen chat controlled by mobile_chat_fix_v10.js */` (файла нет; блок чатов, информационно).

Версионные/legacy-имена, ещё живые:

- `.tasks-redesign-page` — CSS 999, 4953, 5648, 5685; HTML 816 (`<main id="tasksView" class="tasks-page view hidden tasks-redesign-page">`) — дублирует `#tasksView`;
- `knowledge-layout-v51` — только HTML 908, **ни одного правила CSS/JS → мёртвый класс в разметке**;
- `.legacy-logout-btn` — HTML 66 и 91, ни CSS, ни JS → мёртвый класс;
- `.task-create-form-v2` — CSS 658, HTML 225 (чаты, информационно).

### C7. Совместимость и прочее

- Вендорные свойства: `-webkit-overflow-scrolling` ×5 (1848, 2416, 4427, 5654, 5988 — устарело с iOS 13), `-webkit-appearance` ×4,
  `-webkit-tap-highlight-color` ×2, `-webkit-line-clamp`/`-webkit-box-orient` ×2 (нужны), `-webkit-transform` 2570 (лишнее),
  `-webkit-backdrop-filter` 3389, `::-webkit-scrollbar*` — 9 селекторов (нестандартный скроллбар, `scrollbar-color` не задан).
- `transition: all` — 0. `outline: none` — 5 (164 базовые поля; 1587, 1670, 2789, 5906 — чаты), во всех есть замена `box-shadow`/`border` в том же правиле.
- `backdrop-filter`: 6 — `.extra-panel` 649 (чаты), `.app-nav` 2241/3388/3594 (три раза, blur 16→16+saturate→18), **`html[data-theme] .panel-card, .sidebar, .conversation, …` 3480 `blur(18px)`** — размытие на каждой карточке всех разделов (общее с чатами; производительность — этап 2).
- Breakpoints: 10 различных значений в 47 блоках — `≤900` **26**, `≤760` 6, `≤560` 4, `≤1100` 3, `≤1280` 2, `≥901` 2, `≤1080` 1, `≤520` 1, `≥1200` 1, `1101–1250` 1. Нет единой сетки: разделы «поставки/задачи» ввели 760/1100/1250, база — 560/900/1080/1280.

---

## HTML

| Метрика | Значение |
| --- | --- |
| Размер | 1211 строк, 85 780 байт; `<main>` 11 шт.: `chatsView` 12.6 КБ, `ozonFboView` 14.2 КБ, `analyticsView` 7.3 КБ, остальные 1.8–5.9 КБ |
| id | 356, дубликатов нет (пинится `test_ozon_fbo_ui::test_no_duplicate_ids_in_index`) |
| Кнопки | 106, все с `type`; inline-обработчиков 0, `style=""` 0 (совместимо с CSP `script-src-attr 'none'`) |
| Элементов с классом `hidden` в разметке | 73 |
| `role` | `status` 4, `dialog` 4, `menu` 1, `search` 1 |

**Inline SVG**: 34 штуки, **17 уникальных** наборов `<path d>`; суммарно 4.5 КБ. Повторы: шеврон `M9 6l6 6-6 6` ×7
(107–114, лист «Ещё»), иконка выхода ×4 (66, 91, 116, 1178), и 8 пар «десктоп-навигация ↔ мобильный лист»
(63↔113, 64↔114, 75↔109, 81↔110, 82↔111, 83↔112, 84↔115, 150↔840). При этом 6 главных пунктов навигации (57–62) рисуют иконки
**через `<img src="/static/icons/nav-*.svg">`** (6 дополнительных запросов), остальные 5 — inline SVG → две системы иконок в
одной панели. В `app.js` ещё 7 `<svg` в шаблонах (3812 чаты, 5818–5823 задачи, 7455/7458 шаблоны ответов, 7906 сотрудники).
`icons/Gradient-Mesh-27.jpg` (25 КБ) нигде не используется.

**Emoji и глифы**:

- `<select id="chatStatusColor">` 1075–1081: опции `🟠 🔵 🟣 🟢 🔴 ⚪` без текстовых подписей (для скринридера и в Windows-шрифтах
  это «оранжевый круг»; значение цвета передаётся только пиктограммой);
- глифы в кнопках: `×` — 104, 173, 289, 457, 507, 700, 866, 1002, 1029 (+ JS 1169, 2060, 4790); `←` — 192, 938, 950, 1191;
  `✕` — 1194; `⋯` — 208 (+ JS 4080, 4859); `＋` (fullwidth) — 288, 304; `✦` — 49, 306; `✎` — 939 (+ JS 2057); `✨` 213/275, `➤` 307
  (чаты). Два разных «закрыть» (`×` и `✕`), рядом с SVG-иконками — три визуальных языка иконок. В JS ещё 22 emoji
  (✓/✉ в тостах 1166, чеки доставки 4341–4361 — чаты).

**`aria-hidden`** — 47 вхождений; декоративные иконки корректны. Проблемные: заголовки grid-«таблиц» настроек
`.status-settings-header` (1087) и `.task-type-settings-header` (1119) объявлены `aria-hidden="true"` — колонки «Статус /
Порядок / Активность / Действия» недоступны AT, а сами строки не являются таблицей; `<main id="ozonFboView" … aria-hidden="true">`
(532) — единственный `<main>` с предустановленным атрибутом, остальные получают его из `showView` (7322) → разный стартовый DOM.

**Две (три) кнопки выхода**: `#logoutBtn` (91, `secondary refresh-btn legacy-logout-btn`), `#mobileLogoutBtn` (66, `nav-btn mobile-logout-btn legacy-logout-btn`)
и пункт `data-mobile-action="logout"` в листе «Ещё» (116) — три контрола одного действия, класс `legacy-logout-btn` ничем не стилизован.

**Скрытый input как состояние**: `#taskBucketFilter` (857, `type="hidden" value="active"`) читается в `loadAllTasks` (5731) и
слушается на `change` (8355), но **нигде не записывается** — событие `change` у hidden-инпута не возникает, значение всегда `active`.

**Версии ресурсов** — четыре разных значения в одном документе:

| Где | Значение |
| --- | --- |
| `styles.css?v=` (18), `app.js?v=` (1209), `<meta name="arti-build-version">` (13), `sw.js:ARTI_CRM_SW_VERSION` | `v96-ozon-fbo-supplies-20261029` |
| `manifest.webmanifest?v=` (7), `apple-touch-icon?v=` (12) | `v57-mobile-questions-inline-and-dark-analytics-20260701` |
| `theme-init.js?v=` (17) | `v79-strict-csp-secure-cookie-20260708` |
| `app/main.py:91 CRM_BUILD_VERSION` (отдаётся в auth-ответах 2851, 2863) | `v80-login-bruteforce-lockout-20260708` |

Тесты сверяют только `sw.js ↔ styles/app` (`test_ozon_fbo_ui:223`, `test_reply_templates_ui:44`, `test_chat_task_visual_contract:139`).
Кэш при этом всё равно `no-store` (см. Backend), так что `?v=` сейчас ничего не даёт.

**Заголовки**: 2×`h1` (36 логин, 51 бренд), 14×`h2`, 26×`h3`; пропусков уровней нет; 5 пустых заголовков заполняются JS
(196, 736, 789, 934, 1199).

**Поля без подписи**: из 103 контролов 78 имеют `<label>`/`aria-label`/`aria-labelledby`, **25 — только placeholder**:
`loginUsername` 38, `loginPassword` 39, `knowledgeSearch` 904, `knowledgeCategoryTitle` 919, `knowledgeArticleTitle` 954,
`knowledgeArticleCategory` 955, `knowledgeArticleTags` 956, `knowledgeArticleContent` 963, `newUserUsername` 1005,
`newUserDisplayName` 1006, `newUserPassword` 1007, `newUserRole` 1008, `chatStatusTitle` 1074, `chatStatusSort` 1083,
`taskTypeTitle` 1110, `taskTypeCommentLabel` 1111, `taskTypeSort` 1115 (17 в зоне работы) + 8 в чатах (251–309,
`reviewReplyText` 751, `questionAnswerText` 804 — отзывы/вопросы, в зоне работы).

Внешние ссылки: 540 `rel="noopener noreferrer"`, 798 `rel="noreferrer"` — ок.

---

## JavaScript

### J1. Структура

- 484 функции верхнего уровня (98 `async`), 163 глобальных объявления: **122 `let` (изменяемые)**, 15 `const`-контейнеров
  (`VALID_VIEWS`, `VIEW_ROUTES`, `ROUTE_VIEWS`, `marketplaceNames`, `taskStatusNames`, `reviewStatusNames`, `questionStatusNames`,
  `chatStatusColors`, `taskUiStatusOptions`, `ANALYTICS_WEEKDAY_NAMES`, `OZON_FBO_*` ×4, `ozonFbo`), 26 `const`-примитивов.
  Из 122 мутабельных ~75 — состояние чатов/синхронизации (1–23, 202–238, 282–299, 2517–2519; информационно), ~25 — уведомления/звук/push
  (250–281), остальные: `reviews/currentReview*` 211–213, `questions/currentQuestion*` 214–217, `currentUser/assignees/usersCache` 218–220,
  `knowledge*` 239–244, `supplyPlanning*` 247–249, `taskTypes` 283, `yandexOAuth*` 44–47, `chatSettings/statusNames` 700/710.
  Неиспользуемые глобалы: `FRONTEND_OZON_QUESTIONS_SYNC_INTERVAL_MS` 2641, `ACTIVE_CHAT_UI_REFRESH_INTERVAL_MS` 2643 (блок чатов).
- Свойства `window`: только `window.__kbSearchTimer` и `window.__kbResizeTimer` (init 8800/8802) — таймеры debounce поиска/ресайза базы знаний, живут на `window` вместо модульных `let`.
- Функции > 120 строк: **`init` 701** (8256–8956), `renderMessages` 191 (4051–4241, чаты), `loadChats` 137 (3024–3160, чаты).
  Далее: `createChatReadStateController` 110, `createChatPinStateController` 101 (чаты), **`bindTaskBoardCardActions` 100** (5626–5725),
  `openChat` 95, `showView` 88 (7283–7370, общее), **`renderAnalyticsDrilldown` 87** (6518–6604), **`renderAllTasks` 81** (5760–5840),
  **`renderKnowledgeCategories` 80** (6066–6145), `renderChatList` 76, `api` 74, `renderTasks` 70 (чат-задачи), `saveReplyTemplateFromComposer` 69,
  `loadNotifications` 63, `renderChatSettingsLists` 63 (2030–2092), `setupAuthUi` 63, `renderOzonFboRun` 61, `submitOzonFboRun` 59,
  `openReview` 52, `handleChatStatusListClick` 51, `calculateSupplyPlanningRow` 50, `bindOzonFboControls` 43.
- **`init()`**: 701 строка; 42 `addEventListener` + 88 `bind(...)` = 130 привязок на 137 различных id; вызывает 83 разные функции;
  3 `setInterval` (8947/8950 — литерал `60000`, 8953 — константа); 33 `await`; **9 прямых `api()`** внутри inline-обработчиков форм
  (8470 — POST воронок, см. J5; 8483, 8613, 8763, 8824, 8880); 17 `notify`; 4 debounce-таймера. Обязанности: маршрут при старте,
  привязки всех 11 разделов, submit-логика настроек/сотрудников/знаний, интервалы, слушатели `visibilitychange/focus`.

### J2. Дублирование

| Семейство | Функции (строка, размер, ссылок) | Наблюдение |
| --- | --- | --- |
| Бейджи статуса | `statusBadge` 1960 (общее с чатами), `reviewStatusBadge` 4895, `questionStatusBadge` 5080 (**0 ссылок**), `ozonFboStatusBadge` 9119, `ozonFboToneBadge` 9458, `taskUiStatusLabel/Class` 5541/5548, `waitingResponseBadge` 1775 (чаты) | `reviewStatusBadge` и `questionStatusBadge` **побайтно одинаковы** кроме словаря имён; три разных соглашения о классах: `status-badge status-X`, `review-status review-status-X`, `status-badge ozon-fbo-state-X` |
| Даты | `parseDate` 1583, `formatDateTime` 1593, `formatChatTime` 1605, `formatMessageTime` 1616 (все общие с чатами), `datetimeLocalValue` 1782, `ozonFboFormatTime` 9111 + свой `ozonFboParseServerTime` | `ozonFboFormatTime` повторяет `formatChatTime`/`formatMessageTime` (время + `dd.mm`) со своим парсером |
| Модалки | `openUserCreateModal` 7799 / `close…` 7807, `openUserEditModal` 7824 / `close…` 7843, `openOzonFboDecision` 9558 / `close…` 9575, `openKnowledgeModal` 6014 / `close…` 6007, `showKnowledgeEditor` 6234 | Три стратегии фокуса: users — `setTimeout(…focus, 30)` без возврата фокуса; FBO — запоминает `decisionOpener` и возвращает; knowledge — фокусом не управляет, `aria-modal` нет. Escape обрабатывают **5 отдельных слушателей `document.keydown`** (9015 лист «Ещё», 9033 userCreate, 9040 userEdit, 9728 FBO, 8812 knowledge в init); backdrop — три разных обработчика (7814, 7851, 9723); кнопка закрытия knowledge-модалки (8791) вызывает `showKnowledgeEmpty()+renderKnowledgeArticles()` вместо `closeKnowledgeModal()` |
| Debounce | `scheduleCurrentChatMetaSave` 697 (чаты), поиск задач 8359, поиск знаний 8800, ресайз знаний 8802, плюс таймеры 2750/2792/2836/2924 (чаты) | 4 ручных `clearTimeout/setTimeout` без общего `debounce()` |
| Ошибки загрузки | `notify('Ошибка загрузки …')` 13 мест; в `showView` **7 одинаковых хвостов** `.catch(err => notify('Ошибка загрузки X', String(err.message \|\| err)))` (7353–7364); «Ошибка загрузки аналитики» повторена 4× (7353, 8409, 8416, 8421) | Заголовки `notify` повторяются: «Тип задачи» 7, «Шаблоны» 6, «Технические настройки» 4, «Готово» 4 |
| Представления | `VALID_VIEWS` 26 + `VIEW_ROUTES` 27 + `ROUTE_VIEWS` 173 + литералы `viewMap`/`navMap` внутри `showView` (7307–7345, 22 `$()`-поиска при каждом переключении) + `data-mobile-view` в HTML | Список разделов закодирован в 5 местах; `activeView === '…'` 30 сравнений, `showView('…')` 18 вызовов |

### J3. Смешение ответственности

- `innerHTML` присваивается 85 раз в 40 функциях (`hydrateAssigneeSelects` 4, `renderTasks` 4, `renderAnalyticsDrilldown` 4, `render*` по 2–3,
  `loadUsers` 2, `openReview`/`openQuestion` 2); `insertAdjacentHTML` 0; `createElement` 38 — два стиля рендера сосуществуют.
- Загрузка + рендер + привязка в одном теле: `openReview` (4945–4996), `openQuestion` (5310–5352), **`loadUsers` (7883–7911 — сам
  строит таблицу)**, `handleOzonFboFileChange`. Функции с `api()` + `innerHTML` + слушателями одновременно: 0 (привязки вынесены в `bind*`).
- Привязка «на каждый элемент после `innerHTML`»: **15** мест `querySelectorAll(...).forEach(el => el.addEventListener(...))`
  (`renderNotifications` 1182/1192, `bindTaskCardActions` 1911 — общее, `renderComposerAttachments` 4793 — чаты, `bindInlineQuestionForms` 5213,
  `bindTaskBoardCardActions` 5638/5688, `renderKnowledgeCategories` 6130/6139, `renderKnowledgeArticles` 6181, `renderAnalyticsHourly` 6505,
  `renderAnalyticsDrilldown` 6564, `init` 8552/8586; всего 30 `addEventListener` внутри циклов). Делегирование на контейнер при этом
  уже применяется (`handleChatListClick`, `handleUsersListClick` 8073, `handleChatStatusListClick` 8129, FBO 9723, `document.click` 5843).
- Повторные поиски DOM: **57** конструкций `if ($('x')) $('x').prop = …` и 159 `$('x')?.` — каждая привязка/рендер ищет элемент дважды;
  `bind()` (7628) при отсутствии элемента пишет `console.warn('Element not found')` и молча продолжает.

### J4. Обработка ошибок

87 вызовов `api()/apiForm()`: 48 `await` внутри `try`, **30 `await` без `try`** (ошибка уходит вызывающему: `loadReviews` 4913,
`loadQuestions` 5270, `loadKnowledge` 6155, `loadKnowledgeArticles` 6168, `loadUsers` 7888, `loadYandexOAuthManagedLinks` 7973,
`patchTask` 1879, `deleteTask` 1899, `saveTaskTypeRow` 2364, `deleteTaskTypeRow` 2383, `openReview` 4947, `openQuestion` 5313,
`loadAllTasks` 5756, `init` ×6: 8470, 8483, 8613, 8763, 8824, 8880 …), 4 `return api(`, 1 `then/catch`, 4 внутри `Promise.all` (3686/3687 чаты, 6364/6365 аналитика).

Промисы без обработчика (вызов `async`-функции без `await`/`.catch`): 21 место; из них вызывающие функции без собственного `try`:
`loadTaskTypes({ silent: true })` в ветке `techSettings` `showView` (7366 — отличается от ветки `tasks`, где есть `.catch`),
`loadKnowledgeArticles()` 6136 (клик по категории), `openQuestion(question.id)` 5291, `openNotification(...)` 1186, `toggleNotificationsPanel(true)` 7146,
`loadQuestions()` 8912 и **8990 (верхний уровень модуля)**, `registerPwaServiceWorker()` 1008/1104, `openChat` ×3 (чаты).
`loadSupplyPlanning()` — синхронная (localStorage), `loadOzonFbo()` ловит сам.

Пустые `catch (_) {}` — 7: 134 `readPreferredCrmTheme`, 161 `applyCrmTheme`, 775 `unlockNotificationSound`, 1449 `apiForm`,
7231 `getInitialRouteView`, 7242 `rememberRouteView`, 8219 `setupAuthUi` (всё вокруг `localStorage`/`AudioContext` — допустимо, но без комментария «почему»).
Всего `catch` 83; `.catch(` 74 против `.then(` 12 — стиль «await + хвостовой catch».

`console.*`: 55 `warn`, 2 `error`, 1 `log` — **`console.log(title, data)` внутри `notify()` (755)**: каждый тост в проде пишется в консоль
(общее с чатами). Предупреждения, которые видит только консоль, хотя пользователь ждёт результат: 1824 (исполнители), 1991 (статусы чатов),
2134 (типы задач), 6737/6749 (состояние поставок в localStorage), 7389/7398/7482/7609 (шаблоны — fallback в localStorage без уведомления),
7732, 8266/8267/8909 (init), 9154 (история FBO), 9398/9415 (poll FBO). Остальные 30+ — фоновые таймеры чатов (информационно).

### J5. Неиспользуемый и недостижимый код

| Кандидат | Строка | Уверенность | Пометка |
| --- | --- | --- | --- |
| `readCookieValue` | 50 | высокая (0 ссылок, нет в HTML/строках) | CSRF теперь через `csrfToken` |
| `markAllNotificationsRead` | 1312 | высокая | |
| `questionAnswerBadge` | 5161 | высокая | |
| `questionStatusBadge` | 5080 | средняя (совпадает только с id в HTML) | дубликат `reviewStatusBadge` |
| `taskPatchStatusValue` | 5558 | высокая | общий блок 5505–5564 |
| `funnelOptions` | 1973 | высокая | блок 1954–1981 общий с чатами |
| `chatSubtitleParts`, `hasRawFlag`, `findRawStatusValue` | 1649, 4492, 4505 | высокая | чаты — информационно |
| Воронки («временно скрыты», 8081) | `handleChatFunnelListClick` 8080 (пустое тело), submit-обработчик `chatFunnelForm` 8465–8473 с `api('/api/chat-settings/funnels', POST)`, `bind('chatFunnelsList')` 8495, `bind('funnelFilter')` 8327, `chatSettings.funnels` 700, 2011–2014, параметр `funnelId` в `statusOptions` 1977 | высокая: в `index.html` **0 вхождений `funnel`** — элементов нет, ветки не достижимы | `funnelFilter` входит в список защищённых id чатов (exclusions §2.1) — трогать только 8465–8495 (тех. настройки) |
| `#taskBucketFilter` | HTML 857, JS 5731, 8355 | высокая | hidden-input без писателя (см. HTML) |
| `.legacy-logout-btn`, `knowledge-layout-v51` | HTML 66/91, 908 | высокая | классы без CSS/JS |

### J6. Магические числа и строки

- Статусы литералами: `'active'` 27, `'archived'` 9, `'in_progress'` 10, `'done'` 5, `'new'` 5, `'closed'` 4, `'open'` 3, `'awaiting_answer'` 2,
  `'finished'/'failed'/'stopped'/'interrupted'/'queued'/'running'` в FBO (есть словари `OZON_FBO_STATUS_LABELS`, но сравнения строками);
  сравнения `=== '…'`: `'archive'` 19, `'chats'` 19, `'questions'` 12, `'admin'` 11, `'mine'` 9, `'cargo'` 7, `'dark'` 5, `'ozon'` 5, `'internal'` 5.
- Роли: `'admin'` 11, `'manager'` 6, `'viewer'` 3 (`roleLabel` 7820 + `currentUser?.role !== 'admin'` — пинится тестом `test_yandex_oauth_links_admin:725`).
- Разделы: `'chats'` 36, `'questions'` 19, `'tasks'` 13, `'ozonFbo'`/`'knowledge'` 11, `'users'` 10, `'analytics'/'supplyPlanning'/'techSettings'/'profile'` 9.
- Интервалы: 9 констант `*_MS` сгруппированы в 2639–2647 (чаты/уведомления), `OZON_FBO_POLL_MS` 9051, `CHAT_LIST_LOAD_THRESHOLD_PX` 229;
  **24 из 32 таймеров — числовые литералы**: `30` ×5 (фокус в модалках 7495, 7520, 7804, 7840, 9572), `250` ×4, `60000` ×2 (init 8947/8950),
  `8000` (checkAuth 7737), `1600` (AI 4880), `400/300/260/220/120/80/50/40/0`.

### J7. Консистентность

- 98 `async`-функций, 252 `await`; `.then(` 12 vs `.catch(` 74; `$` объявлен (`const $ = (id) => document.getElementById(id)`), 593 вызова,
  2 «сырых» `getElementById`; `querySelector` 80, `querySelectorAll` 35.
- 80 HTML-шаблонов (template literal с тегами) общим объёмом 600 строк. Самые длинные: `renderChatSettingsLists` 52 строки (2039–2090, тех. настройки),
  `renderAllTasks` 47 (5781–5827), `renderTasks` 39 (чаты), `supplyPlanningRowHtml` 28, `renderAnalyticsDrilldown` 27+24+12, `renderAnalyticsDaily` 24,
  `renderChatList` 23, `questionInlineDetailHtml` 23, `renderYandexOAuthManagedLinks` 17, `renderTaskTypeSettingsList` 16, `taskBoardEditPanelHtml` 16, `loadUsers` 15.
- Маршрутизация строками: hash → `ROUTE_VIEWS` → `showView(view)` → литералы `viewMap`/`navMap` → `body.dataset.activeView` → CSS
  `#…View.hidden`; тест `test_ozon_fbo_ui:88–92` пинит фрагменты `'supplyPlanning', 'ozonFbo', 'tasks'`, `ozonFbo: 'fbo',`, `ozonFbo: $('ozonFboView'),`.

### J8. Гигиена безопасности (только отчёт)

328 интерполяций `${…}` в 80 HTML-шаблонах: 185 обёрнуты `escapeHtml` на месте, 91 безопасны по построению (числа, вложенные шаблоны,
helper-функции), 52 помечены эвристикой; ручная проверка помеченных: почти все — переменные, экранированные строкой выше
(`renderNotifications` 1163–1165, `knowledgeArticleCardHtml` 6041–6042, `renderYandexOAuthManagedLinks` 7933, `renderKnowledgeCategories` 6087).
Остаток — значения-перечисления с бэкенда, попадающие **в атрибут `class` без allow-list**: `chat.marketplace` 3814 (и как подпись-фолбэк; чаты — информационно),
`calc.status` 6974 (поставки), `tone` в `ozon-fbo-tone-${tone}` 9461 (ключ словаря, безопасно), `colorClass` 1964 — санируется regex.
`escapeHtml` 194 вызова, `textContent` 95. Сгенерированные ссылки `target="_blank"`: одна (5184) с `rel="noreferrer"` — ок.
Inline-обработчиков нет, что согласуется с CSP `script-src-attr 'none'` (main.py 482).

---

## Backend touchpoints

Только для сведения; по `audit/chat-exclusions.md` §4 заголовки кэша/сжатия меняются после отдельного согласования владельцем.

1. **Порядок middleware** (`app/main.py`): декораторы `@app.middleware("http")` на строках 393 (`security_guard_middleware`),
   428 (`require_auth_for_api`), 512 (`add_fastfox_cache_headers`), 776 (`no_cache_frontend`). Starlette оборачивает так, что
   последний зарегистрированный — внешний: запрос идёт `no_cache_frontend → add_fastfox_cache_headers → require_auth_for_api → security_guard`,
   ответ — в обратном порядке. Следствия:
   - для `/static/*` `add_fastfox_cache_headers` ставит `Cache-Control: public, max-age=3600` (519–522, `setdefault`), затем внешний
     `no_cache_frontend` **перезаписывает** на `no-store, no-cache, must-revalidate, max-age=0` + `Pragma: no-cache` (776–782) —
     `?v=` в `index.html` и кэш в 1 час не работают, 677 КБ фронтенда качаются при каждом открытии;
   - `_apply_security_headers` выполняется дважды на штатном пути (408 и 529) и один раз на ранних возвратах (401/403/CSRF);
   - HTTPS-redirect живёт внутри «кэш»-middleware (513–515).
   Легитимная правка рефакторинга: одна политика кэша в одном месте (`no-store` для `/`, долгий `max-age, immutable` для версионированных `/static/*`),
   один вызов security-headers. Нужен тест на `Cache-Control` для `/static/app.js` и `/`.
2. **`CRM_BUILD_VERSION = "v80-login-bruteforce-lockout-20260708"`** (main.py:91) отдаётся клиенту (2851, 2863), тогда как фронтенд — `v96`:
   константа отстала на 16 релизов; единый источник версии (meta в `index.html`/`sw.js`/переменная окружения) — часть кандидата №4.

---

## Repo hygiene

| Объект | Факты | Оценка |
| --- | --- | --- |
| `UPDATE_V*_README.txt` в корне | **91 файл**, все в git (89.6 КБ): `V54…V103`, `V103_PATCH3…PATCH32`, 12 именованных патчей; нумерация `V100–V103` не совпадает со схемой версий приложения (`v96`); ни одной ссылки из `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, `docs/` | исторические заметки релизов → `docs/history/` или `CHANGELOG.md`; в корне мешают навигации |
| `docs/V24…V59_*.md` | 31 файл (27 КБ) + 10 версионных аудитов/фиксов (`REFRACTOR_AUDIT_V16.md`, `MOBILE_PERFORMANCE_AUDIT_V17.md`, `LIVE_MESSAGES_AUDIT_V19.md`, `BACKEND_SYNC_FIX_V20.md`, `MOBILE_INTERACTION_PERFORMANCE_V21.md`, `MOBILE_SAFE_FIX_V22.md`, `SELECT_STABILITY_AUDIT_V23.md`, `V24_DEBUG_PLAN.md`, `V25_MOBILE_RENDER_LIST_FIX_AUDIT.md`, `CODE_AUDIT.md` (v84, про backend)) | устаревшие; `V47/V49/V50/V51` — единственные ссылки на `requirements_push.txt` |
| `reset_arti_crm_admin_password_v3.cmd` | Windows-only операционный скрипт в корне, имя с `_v3` | перенести в `scripts/` + описать в `docs/MAINTENANCE.md` |
| `tools/check_project.py` | в git, упомянут в `docs/MAINTENANCE.md`; пороги `app/static/app.js: 2200`, `styles.css: 3500` превышены в 4.4× / 1.9× | инструмент-предупреждение; пороги не соблюдаются |
| `scripts/` (`scan-tracked-secrets.ps1`, `verify-setup.ps1`), `.agents/skills/safe-refactor/`, `.codex/` (config + 3 роли), `prompts/01–04` | **не в git** (игнорируются) | локальный инструментарий; `.codex/` — наследие Codex-сессий |
| `chat_attachments/` | в `.gitignore`, но **3 файла `chat_107_*.jpeg` отслеживаются git** (закоммичены до правила) | вероятно пользовательский контент в истории репозитория — сообщить владельцу, не открывать |
| `app/static/icons/Gradient-Mesh-27.jpg` (25 КБ) | нет ссылок в html/css/js/py | удалить |
| `app/static/icons/nav-*.svg` ×6 | используются 6 кнопками навигации через `<img>`; те же пиктограммы есть inline в листе «Ещё» | выбрать одну систему (кандидат №10) |
| `requirements_push.txt` | необязательная зависимость `pywebpush` отдельным файлом | слить в `requirements.txt` как extra или описать в README |
| `ecosystem.config.js` | pm2-конфиг с хостом `vh20541.ffox.site` | окружение-специфичен, но нужен хостингу — оставить |
| `patch19.patch`, `audit/` | не в git | пользовательский файл / наши материалы, не трогать |

---

## Тесты, пинящие фронтенд

Файлы `tests/`, читающие статику (19): чаты-стражи (§5 exclusions, 11 файлов) + в зоне работы:

| Тест | Что пинит |
| --- | --- |
| `test_ozon_fbo_ui.py` (15) | разметка `#ozonFboView`/`admin-only`, routing-литералы `showView` (`ozonFbo: $('ozonFboView'),`), остановка poll при уходе, делегирование и экранирование FBO-рендера, узел `renderOzonFboRun` в node, **версия `sw.js ↔ styles/app`**, «CSS FBO только из токенов», mobile 1 колонка, **уникальность id в index.html** |
| `test_task_assignee_date_sort.py` (4 UI из 10) | id фильтров задач, popover дат (`taskDueDateFilter*`), `datetime="${escapeHtml(task.due_at \|\| '')}"`, `#tasksView .tasks-ref-icon-btn {` |
| `test_knowledge_image_security.py` (1 UI) | отсутствие `knowledgeArticleImageUrl`, наличие `document.querySelectorAll('.admin-only')` |
| `test_yandex_oauth_links_admin.py` (2 UI) | фрагменты `resetYandexOAuthManagedLinksState` (`list.innerHTML = '';`, `userSelect.innerHTML = '';`), `showApp` (`currentUser?.role !== 'admin'`) |
| `test_auth_login_errors.py` (1 UI) | маппинг кодов ошибок логина в app.js |
| `test_sync_control_security.py` (1 UI) | синхронизация поставок через POST + CSRF-helper |
| `test_chat_task_visual_contract.py` | версия `sw.js ↔ index.html` (139) + чаты |
| `test_reply_templates_ui.py`, `test_ozon_product_context.py` | чаты (CSS composer/шаблонов: пинят `border: 0 !important`, `-webkit-line-clamp: 3`) |

---

## Сводка: топ-15 кандидатов на рефакторинг

Ранжирование: выигрыш в сопровождении × безопасность. «Diff» — ожидаемый размер изменения.

| # | Кандидат | Scope (файлы/строки) | Diff | Риск | Общее с чатами | Тест-стратегия |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Удалить legacy-блок «peak hours thin-bar» аналитики**: все 37 `:has()`, 106 `nth-child`, 34 правила, ~40 `!important`, мёртвые `.peak-hours-*`/`#peakHoursPanel`; эффект уже отменён v59 (`content: none !important`, 4861–4866) | `styles.css` 1857–2087, 2432–2458 | −260 строк | low | нет | тестов нет → добавить характеризационный: `#peakHoursPanel`/`.peak-hours-` отсутствуют в CSS, `#analyticsHourlyChart` стили v59 на месте; скриншот `#analyticsView` до/после (desktop + ≤900) |
| 2 | **Удалить мёртвые классы и версионные имена**: `.task-card-*`, `.task-edit-actions`, `.knowledge-tag`, `.analytics-daily-table`, `.supply-planning-delete`; несуществующие имена из списков v52/v55 (3466/3482/3495/3509/3528/3901); в HTML `knowledge-layout-v51`, `legacy-logout-btn`; `.tasks-redesign-page` → `#tasksView` | `styles.css` 442, 1001, 1098–1103, 1139, 4127, 5439, 999/4953/5648/5685; `index.html` 66, 91, 816, 908 | −80 | low | частично (списки селекторов скина общие — убирать только отсутствующие имена, порядок не менять) | `css_audit.py` → 0 «likely dead» вне чатов; полный pytest (стражи чатов), `test_ozon_fbo_ui::test_no_duplicate_ids_in_index` |
| 3 | **Один блок dark-токенов и удаление 10 пустых theme-restate правил** (3312/3869/3895/4185/5120 → один; 4922, 5799, 5453, 4538, 4173, 3457, 3546, 3561, 3587, 4944); зафиксировать, что базовый `:root` 4–47 перекрыт v52 (и исправить `--crm-bg: #ffffff0000`) | `styles.css` 3270–3349, 3864–3900, 4185–4194, 5120–5130 | −60/+20 | med | **да** (токены) — только перенос без изменения значений; исправление `--crm-bg` согласовать | `test_reply_templates_ui`/`test_ozon_product_context` пинят чат-CSS (не трогать строки); добавить node-тест «каждый `--crm-*` dark-токен объявлен один раз»; браузерная сверка computed-style 6 токенов light/dark |
| 4 | **Единая версия ресурсов**: один источник для `?v=` (styles, app, theme-init, manifest, apple-touch-icon), `<meta arti-build-version>`, `sw.js`, `main.py:CRM_BUILD_VERSION` | `index.html` 7, 12, 13, 17, 18, 1209; `sw.js` 1; `main.py` 91 | ±10 | low | нет | `test_ozon_fbo_ui::test_frontend_cache_version_bumped_and_consistent`, `test_reply_templates_ui::test_frontend_cache_version_is_consistent`, `test_chat_task_visual_contract::test_html_asset_versions_match_the_executed_service_worker` — расширить на manifest/theme-init/`CRM_BUILD_VERSION` |
| 5 | **Разбить `init()` (701 строка)** на `bindTasksView/bindKnowledgeView/bindUsersView/bindTechSettingsView/bindAnalyticsView/bindSupplyView` (по образцу `bindOzonFboControls`), вынести inline submit-обработчики с `api()` (8483, 8613, 8763, 8824, 8880) в именованные функции, убрать мёртвую ветку воронок 8465–8473/8495 | `app.js` 8256–8956 (не-чатовые части), 8080 | перенос ~450 строк, net ≈ −30 | med | **да**: чат-привязки внутри `init` (список id в exclusions) должны остаться побайтно — выносить только остальные | `test_yandex_oauth_links_admin` (`showApp`), `test_knowledge_image_security` (`.admin-only`), `test_task_assignee_date_sort`; новый node-характеризационный тест: множество `bind('<id>'` до/после совпадает и каждый id есть в `index.html` |
| 6 | **Helper загрузки раздела** `runSectionLoader(label, loader)` вместо 7 одинаковых `.catch(err => notify('Ошибка загрузки …'))` в `showView` и 3 повторов в `init` (8409–8421); закрыть необработанный `loadTaskTypes({silent:true})` 7366 | `app.js` 7353–7366, 8405–8425 | −25/+15 | low | `showView` общий — ветки `chats` не трогать, минимальный diff | `test_ozon_fbo_ui:88–101` пинит литералы `viewMap`/routing в `showView` — сохранить; node-тест: при reject загрузчика вызывается `notify` с тем же заголовком |
| 7 | **Удалить мёртвый JS**: `readCookieValue` 50, `markAllNotificationsRead` 1312, `questionAnswerBadge` 5161, `questionStatusBadge` 5080, `taskPatchStatusValue` 5558, `handleChatFunnelListClick` 8080 (+ п.5), `#taskBucketFilter` (HTML 857, JS 5731/8355 → константа) | `app.js`, `index.html` 857 | −60 | low | частично: `chatSubtitleParts`/`hasRawFlag`/`findRawStatusValue`/`funnelOptions` в чат-блоках — не трогать | `node --check app.js`; полный pytest; grep-тест «нет ссылок на удалённые имена» |
| 8 | **Один билдер бейджа** для отзывов/вопросов/FBO (`reviewStatusBadge` 4895 ≡ `questionStatusBadge` 5080, `ozonFboStatusBadge` 9119, `ozonFboToneBadge` 9458) с общей схемой классов; `ozonFboFormatTime` → `formatChatTime`-семейство | `app.js` 4895–4900, 5080–5085, 9111–9123, 9458–9463 | −25/+15 | low | `statusBadge` 1960 и `format*` 1583–1628 общие — не менять, переиспользовать | `test_ozon_fbo_ui::test_render_run_behaviour_in_node`, `::test_rendering_escapes_server_text_and_uses_delegation` пинят разметку FBO-бейджа — выход должен совпасть побайтно |
| 9 | **Один контроллер модалок** (`openDialog/closeDialog`: сохранение/возврат фокуса, `aria-modal`, Escape, backdrop) для `userCreateModal`, `userEditModal`, `ozonFboDecisionModal`, `knowledgeModal`; 5 слушателей `document.keydown` → 1 | `app.js` 6007–6037, 7799–7860, 9558–9585, 9015–9045, 9728, 8791, 8812 | −70/+45 | med | нет | `test_ozon_fbo_ui::test_decision_dialog_lifecycle`, `::test_decision_dialog_is_accessible` (поведение `decisionOpener`); добавить node-характеризацию users-модалок; клавиатурная проверка в браузере |
| 10 | **Спрайт иконок** `<symbol>`+`<use>` для 34 inline SVG (17 уникальных), замена текстовых глифов `× ← ✕ ⋯ ＋ ✎` вне чатов, один способ иконок навигации (`<img nav-*.svg>` ×6 ↔ inline) и удаление `Gradient-Mesh-27.jpg` | `index.html` 57–116, 150, 326, 823, 840, 938–950, 1178–1194; `app.js` 5818–5823, 7906; `styles.css` 3181–3246 | HTML −2.5 КБ, JS/CSS ±40 | low–med | частично: глифы в `#chatsView` (173, 192, 208, 288–307) и `<svg>` в чат-шаблонах не трогать | `test_task_assignee_date_sort::test_task_actions_are_compact_icon_only_controls`, `test_reply_templates_ui` пинит `<svg viewBox="0 0 24 24"` в app.js (чаты); визуально desktop/mobile |
| 11 | **Делегирование вместо `querySelectorAll().forEach(addEventListener)`** в знаниях (6130/6139/6181), аналитике (6505/6564), доске задач (5638/5688), уведомлениях (1182/1192), init (8552/8586) — по образцу `handleUsersListClick`/FBO | `app.js` указанные функции | −40/+40 | med | `bindTaskCardActions` 1911 общее, `renderComposerAttachments` чаты — не трогать | `test_ozon_fbo_ui::test_rendering_escapes_server_text_and_uses_delegation` как эталон; node-тесты кликов по `data-*` до/после; проверка клавиатуры |
| 12 | **Литералы → токены в не-чатовых слоях**: цвета (199 различных), радиусы (28 px-литералов, 7 = токенам), тени (14 одноразовых); ввести шкалу `--crm-font-*` (12 размеров, 58 деклараций ≤12px) | `styles.css` 1802–2201, 4386–4547, 4818–5100, 5101–5260, 5261–5641, 5642–5868, 5916–6095, 6096–6257 | ~150 строк изменено, net 0 | med | токены общие — значения не менять, только заменять литералы | `test_ozon_fbo_ui::test_styles_use_tokens_without_ai_slop` (FBO-блок — образец); скриншоты каждого раздела light/dark; `css_audit.py`: число литералов вне чатов ↓ |
| 13 | **Снять `!important` в не-чатовых мобильных/тематических слоях** (v59 76, v81 knowledge 31 + supply 21 + analytics 11, v57 16, v58 13) через scoped-селекторы `main#…View` вместо перебивания catch-all dark-скина | `styles.css` 4386–4547, 4548–4817, 4818–4949, 5916–6095 | ~140 деклараций | med–high | нет (блоки разделов), но они перебивают общий скин v55/v56 — порядок каскада критичен | тестов нет → до правки зафиксировать computed-style ключевых элементов (node недостаточно; браузер 375/768/1280, light+dark); `css_audit.py`: `!important` по блокам ↓ |
| 14 | **Разделить fetch/render**: `loadUsers` (7883–7911 → `renderUsers`), `openReview` (4945), `openQuestion` (5310), `renderChatSettingsLists` (52-строчный шаблон 2039–2090 → подфункции строки статуса) | `app.js` указанные строки | net 0 | low | нет (`renderChatSettingsLists` — тех. настройки) | `test_yandex_oauth_links_admin:719–720` пинит `list.innerHTML = '';`/`userSelect.innerHTML = '';` (сохранить); добавить node-характеризацию строки сотрудника и строки статуса |
| 15 | **Backend (по согласованию)**: одна политика `Cache-Control` для `/static/*` вместо 519–523 + 776–782, один вызов `_apply_security_headers`, `CRM_BUILD_VERSION` из единого источника | `app/main.py` 393–530, 776–782, 91 | −15/+10 | low технически, требует согласования (§4 exclusions, касается ответов чатов) | нет кода чатов, но middleware общие | `tests/test_sync_control_security.py`, auth/security-тесты заголовков; новый тест `GET /static/app.js` → ожидаемый `Cache-Control`, `GET /` → `no-store` |

Вне топа, но зафиксировано: `console.log` в `notify()` (755, общее с чатами); `backdrop-filter: blur(18px)` на всех `.panel-card` (3480, общее);
25 полей без `<label>` (17 в зоне работы); emoji-опции `#chatStatusColor`; `aria-hidden` на заголовках grid-таблиц настроек (1087, 1119);
три контрола выхода; 10 breakpoint-значений без сетки; `-webkit-overflow-scrolling` ×5.
