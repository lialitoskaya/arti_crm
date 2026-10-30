# Этап 3. Качество кода (кратко; полная версия с измерениями — `audit/details/code-quality-full.md`, скрипты — `audit/details/tools/`)

База `38b34e3`. Аудит остановлен владельцем в экономном режиме 2026-10-30; ниже — только уже собранные факты.

## CSS (`styles.css`, 6 484 строки, 1 092 правила)

- Файл — 19 версионных слоёв (v11…v96) поверх базы; 481 `!important` (177 внутри `html[data-theme]`-правил), 330 theme-префиксов,
  155 правил начинаются с `html[data-theme]`, из них 47 — чистый override базы, 10 — повтор тех же значений без эффекта.
- Базовый блок токенов `:root` (4–47) для 31 цвета мёртв: перекрыт `:root, html[data-theme="light"]` (3272); dark-токены объявлены в 5 блоках.
  `--crm-bg: #ffffff0000` — невалидное значение.
- Legacy-блок «peak hours» аналитики (1857–2087, 2432–2458): все 37 `:has()`, 106 `nth-child`, ~40 `!important`; его эффект отменён слоем v59 (4861–4866). ≈260 мёртвых строк.
- 32 «likely dead» класса (`.task-card-*`, `.knowledge-tag`, `.supply-planning-delete`, имена из списков v52/v55 без элементов и др.);
  в разметке мёртвые `knowledge-layout-v51`, `legacy-logout-btn`, `tasks-redesign-page` (дублирует `#tasksView`).
- Магические значения: 199 различных цветовых литералов вне токен-блоков; 28 px-литералов радиусов (7 равны токенам); 14 одноразовых теней;
  12 размеров шрифта без шкалы, 58 деклараций ≤12px; 10 брейкпоинтов без единой сетки (26 блоков `≤900px`).
- Дубли селекторов: 18 точных (`:root` ×6, `.supply-planning-filters` ×6, `#tasksView .tasks-ref-toolbar` ×6…), 177 пар селектор+свойство объявлены повторно.

## HTML (`index.html`)

- 17 уникальных inline SVG из 34 (17 дублей); две системы иконок навигации (`<img nav-*.svg>` ×6 и inline); текстовые глифы `× ← ✕ ⋯ ＋ ✎ ✦`; emoji-опции `#chatStatusColor`.
- 25 полей только с placeholder (17 вне чатов); шапки grid-«таблиц» настроек с `aria-hidden`; `#taskBucketFilter` — hidden-input, который никто не пишет.
- Четыре версии ресурсов в одном документе (`v96`/`v57`/`v79`) и `CRM_BUILD_VERSION = v80` в `main.py`; кэш при этом `no-store`.
- Три контрола «Выйти»; `Gradient-Mesh-27.jpg` не используется.

## JavaScript (`app.js`, 9 731 строка, 484 функции)

- 122 изменяемых глобала; `init()` — 701 строка, 130 привязок, 9 inline `api()`-обработчиков; функции >80 строк: `bindTaskBoardCardActions`, `renderAnalyticsDrilldown`, `renderAllTasks`, `renderKnowledgeCategories` (вне чатов).
- Дубли: `reviewStatusBadge` ≡ `questionStatusBadge` (последняя не используется); три соглашения классов бейджей; `ozonFboFormatTime` повторяет семейство `format*Time`; 3 стратегии фокуса модалок и 5 `document.keydown` на Escape; 7 одинаковых `.catch(notify('Ошибка загрузки …'))` в `showView`.
- Ошибки: 30 `await api()` без `try`; 21 промис без обработчика (в т.ч. `loadQuestions()` на верхнем уровне 8990, `loadTaskTypes` в ветке techSettings); `notify()` = `alert` + `console.log`; 15 мест `querySelectorAll().forEach(addEventListener)` вместо делегирования.
- Мёртвый код: `readCookieValue`, `markAllNotificationsRead`, `questionAnswerBadge`, `questionStatusBadge`, `taskPatchStatusValue`, ветка воронок (`handleChatFunnelListClick`, submit `chatFunnelForm`), привязки `analyticsWorkHoursBtn`/`saveChatSettingsBtn`.
- Магические строки: статусы/роли/разделы литералами (`'archive'` ×19, `'admin'` ×11…), 24 из 32 таймеров — числовые литералы.
- Безопасность (только отчёт): экранирование соблюдается; остаток — enum-значения бэкенда в атрибуте `class` без allow-list (`calc.status` в поставках).

## Backend touchpoints (только для сведения)

- Две конфликтующие политики `Cache-Control` (`main.py:519–523` и `776–782`), `_apply_security_headers` дважды на штатном пути, HTTPS-redirect внутри кэш-middleware.

## Repo hygiene

- 91 `UPDATE_V*_README.txt` в корне и 31 `docs/V*.md` — исторические заметки без ссылок; `reset_arti_crm_admin_password_v3.cmd` в корне;
  три `chat_attachments/chat_107_*.jpeg` отслеживаются git несмотря на `.gitignore` (сообщить владельцу, не открывать);
  `tools/check_project.py` пороги размера превышены в 4.4×/1.9×.

## Топ кандидатов на рефакторинг (когда будет ресурс)

1. Удалить legacy peak-hours (−260 строк CSS, low). 2. Мёртвые классы/имена (low). 3. Один блок dark-токенов + валидный `--crm-bg` (med, токены общие).
4. Единая версия ресурсов (low). 5. Разбить `init()` на `bind<Section>View` (med; чат-привязки побайтно сохранить). 6. Helper загрузки раздела вместо 7 `.catch` (low).
7. Мёртвый JS (low). 8. Один билдер бейджа (low). 9. Один контроллер модалок (med). 10. Спрайт иконок (low–med).
11. Делегирование событий (med). 12. Литералы → токены (med). 13. Снять `!important` в слоях разделов (med–high). 14. Разделить fetch/render (low). 15. Одна политика кэша (согласование).
