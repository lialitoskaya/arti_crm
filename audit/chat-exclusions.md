# Исключения: раздел чатов

Дата фиксации: 2026-10-30. База: коммит `38b34e3` (ветка `feat/ozon-fbo-supplies`, текущий production).

Раздел чатов (`#chatsView`, маршрут `#chats`, открытие чата `#chats/<id>`) **не меняется** в рамках
`refactor/ui-ux-performance`: ни разметка, ни стили, ни JS, ни API, ни backend-модули. Фронтенд проекта
собран без сборщика и живёт в трёх общих файлах, поэтому исключение задано не списком файлов, а списком
областей внутри файлов плюс списком backend-модулей. Для общих компонентов действует правило: если класс,
токен или функция используется и в чатах, и в других разделах, её нельзя править напрямую — для остальных
разделов создаётся отдельный вариант (scoped-селектор по `main.view` раздела или отдельная функция).

## 1. Разметка: `app/static/index.html`

| Строки | Что | Статус |
| --- | --- | --- |
| 122–317 | `<main id="chatsView">`: список чатов, фильтры (scope, маркетплейс, статус, воронка, календарь), поиск по сообщениям, панель диалога, `#extraPanel` (задачи по чату, заметка, клиент), `#replyTemplatesPanel`, composer, AI-панель | **Исключено целиком** |
| 57 | `<button id="navChats">` | Общий shell: кнопку можно стилизовать вместе с остальными `.nav-btn`, но id/handler/`active` не менять |
| 74–75 | `#syncStatus`, `#refreshBtn` (ручное обновление чатов) | Общий shell, поведение не менять |

Элементы с `data-view="tasks"` внутри `#tasksSection` (строка 222) принадлежат чатам.

## 2. Скрипты: `app/static/app.js`

Границы заданы по именам функций верхнего уровня (индекс функций снят с `38b34e3`).

### 2.1 Исключено (код чатов)

| Строки | Функции / содержимое |
| --- | --- |
| 1–23 | Глобальное состояние чатов (`currentChatId`, `chats`, таймеры и флаги refresh/send) |
| 170–299 | Константы и состояние чатов (`CHAT_LIST_BATCH_SIZE`, модели read/pin state и т.п.) — смешанный блок, менять только константы, не относящиеся к чатам, и только после проверки |
| 300–747 | `createChatReadStateController` … `customerLabel`: read/pin state, `hydrateChatMetaControls`, `persistCurrentChatMeta` |
| 1461–1582 | `sleep`, `createClientOperationId`, операции отправки (`sendChatTextOperation`, `uploadCurrentChatImages`, `dispatchComposerCommands`) |
| 1630–1781 | `isImagePlaceholderText`, `previewText`, `chatSubtitleParts`, `directMarketplaceChatUrl`, `renderChatSubtitle`, `waitingDurationText`, `waitingResponseBadge` и соседние (рендер строки списка чатов). Исключение: `pluralRu` (1719) — общая утилита |
| 2423–2584 | Мобильный режим чата: `isMobileChatLayout` … `scrollMessagesToBottom` |
| 2661–2841 | Фронтенд-синхронизация и refresh активного чата (`runFrontendOzonFastSync`, `runFrontendOzonQuestionsSync`, `startFrontendAutoSync`, `refreshActiveChatUi` …). `runFrontendOzonQuestionsSync` используется разделом «Вопросы», но является частью одного механизма — не менять |
| 2842–2858 | `loadStats` (рендерит `#stats` в сайдбаре чатов) |
| 2859–3495 | Поиск по сообщениям, лента чатов, scope, pin/read, `renderChatList`, `openChat`, `paintChatHeader`, `refreshChatListOnly` |
| 3547–3597 | `syncChatDateFilterUi`, `applyChatDateFilter`, `clearChatDateFilter` |
| 3639–3936 | Загрузка и объединение сообщений, `openChat` |
| 3937–4894 | Рендер сообщений, заметки, чеки/статусы доставки, ссылки Ozon, вложения composer, AI-ответ |
| 5391–5504 | Редактор задачи внутри чата (`renderTasks` для `#tasksSection`, `bindTaskChatEditToggle`) |
| 5848–5992 | `#extraPanel`: геометрия, меню, `showExtraPanel` |
| 7179–7224 | Маршруты `#chats/<id>` (`getChatIdFromRouteValue`, `getQuestionIdFromRouteValue` — последняя общая с вопросами, не менять) |
| 7262–7282 | `syncRouteForChat` |
| 7383–7627 | Шаблоны ответов (`#replyTemplatesPanel` внутри composer) |
| 8998–9045 | `autosizeComposerTextarea` и привязки composer |
| внутри `init()` 8256–8997 | Все `bind(...)` для id из `#chatsView` (см. список ниже) и обработчики формы отправки |

Id элементов чата, привязки которых в `init()` не меняются: `messageForm`/composer, `chatSearchToggleBtn`,
`chatSearchClearBtn`, `chatScopeSelect`, `chatImageInput`, `chatHeaderMenuBtn`, `chatDateFilter*`,
`extraMenuBtn`, `attachImageBtn`, `replyTemplate*`, `aiGenerate*`, `aiClearSelectionBtn`, `saveChatBtn`,
`mobileBackBtn`, `marketplaceFilter`, `statusFilter`, `funnelFilter`.

### 2.2 Общий код, который чаты используют (менять нельзя; для остальных разделов делать отдельный вариант)

| Строки | Функции | Примечание |
| --- | --- | --- |
| 24–47 | `VALID_VIEWS`, `VIEW_ROUTES` | Общий роутинг; записи `chats` не трогать |
| 748–758 | `setStatus`, `notify` | Общая обратная связь; формат и селекторы не менять |
| 1320–1460 | `api`, `apiForm`, ошибки API | Транспорт |
| 1583–1628 | `parseDate`, `formatDateTime`, `formatChatTime`, `formatMessageTime` | Форматирование дат |
| 1782–1953 | `taskStatusOptions`, `assigneeOptions`, `loadAssignees`, `hydrateAssigneeSelects`, `renderTaskComments`, `patchTask`, `deleteTask`, `bindTaskCardActions` | Используются и в `#tasksSection` чата, и в разделе «Задачи» |
| 1954–1981 | `chatStatusColor`, `statusBadge`, `activeChatStatuses`, `statusOptions` | Бейдж статуса чата рендерится в списке чатов |
| 3496–3546 | `normalizeDateRangeValue` … `setDateRangePopover` | Общий календарный фильтр чатов и задач |
| 5505–5564 | `getTaskCustomFieldLabel`, `getTaskPrimaryText`, `normalizeTaskStatus`, `taskUiStatusLabel`, `taskUiStatusClass` | Общие подписи задач |
| 7160–7178, 7225–7261 | `normalizeViewName`, `viewFromRouteValue`, `getInitialRouteView`, `syncRouteForView` | Роутинг |
| 7283–7371 | `showView` | Ветки `chats` (`setMobileChatOpen`, `setChatSearchOpen`, `runFrontendSyncSoon`) не менять |
| 7372–7382 | `escapeHtml` | |
| 759–1319 | Уведомления, звук, Web Push (`trackChatMessageSounds` …) | Shell; меняется только презентация тостов/панели, не логика |

### 2.3 В области работы (не чаты)

`4895–5390` отзывы и вопросы; `5565–5847` раздел «Задачи» (доска, фильтры, standalone-форма);
`5993–6293` база знаний; `6294–6664` аналитика; `6665–7114` поставки (beta); `7115–7159` мобильный лист «Ещё»;
`7628–8255` auth/login, профиль, сотрудники, связи Яндекс, настройки статусов и типов задач (презентация в
`#techSettingsView`); `9046–9731` поставки FBO; `init()` — привязки перечисленных разделов.

## 3. Стили: `app/static/styles.css`

### 3.1 Исключённые блоки (по заголовкам файла)

| Строки | Блок |
| --- | --- |
| 1381–1739 | «Chat list: closer to the Figma reference» |
| 1762–1801 | «Chat list width and preview readability fix» |
| 2202–2465 | Mobile fullscreen chat |
| 2466–2618 | v16–v25 runtime/mobile состояния чата |
| 2619–3180 | v27–v40: диалог сообщений, превью изображений, меню, поиск по сообщениям, infinite scroll |
| 3600–3863 | v54: светлая/тёмная тема сообщений и composer |
| 4152–4167 | Dark theme: message bubbles |
| 5872–5915 | v81: внутренние заметки чата |

### 3.2 Исключённые селекторы, где бы они ни встречались (в том числе в общих блоках 1–1357, 3270–3599, 3864–4547, 6258–6365)

`.layout`, `.sidebar`, `.conversation`, `.chat-*`, `.messages`, `.message*`, `.composer*`, `.extra-*`,
`.reply-template*`, `.ai-*`, `.customer-*`, `.note-*`, `.task-create-*`, `.task-form*`, `.task-list`
(список задач в чате), `.task-comment-*`, `.stats`, `.scope-*`, `.mobile-back-btn`, `.mobile-chat-*`,
`body.mobile-chat-open`, `html.mobile-chat-*`, `#chatsView …`, `.marketplace-pill` в контексте чатов,
`.waiting-*`, `.unread-*`, `.pin-*`, `.empty-state` внутри `#chatsView`.

### 3.3 Общие стили, которые нельзя менять напрямую (нужен вариант для остальных разделов)

- Токены `:root`, `html[data-theme="light"]`, `html[data-theme="dark"]` (строки 4–47, 3272–3349): цвета,
  тени, радиусы, `--crm-focus`. Изменение глобального токена меняет и чаты. Вариант для остальных
  разделов — scoped-переопределение токенов на `main.view` каждого раздела (одно место, без цепочки override),
  решение подтверждается владельцем в `04-plan.md`.
- Базовые `button`, `button.secondary/.ghost/.small/.icon-btn`, `input`, `select`, `textarea`,
  `::placeholder`, `:focus-visible` (1–1357 и dark-слой 3864–4547): используются composer и фильтрами чатов.
- `.panel-card`, `.status-badge`, `.date-range-filter-*`, `.empty-state`, `.nav-btn`, `.notification-*`,
  `.sync-card`.

Блоки 1802–2201 (аналитика, тех. настройки), 3181–3269 (иконки навигации, PWA), 4548–4817 (настройки
статусов), 4818–5100 (задачи), 5101–5260 и 6096–6257 (база знаний), 5261–5641 (поставки beta),
5642–5868 (задачи/аналитика), 5916–6095 (мобильные аналитика/знания/поставки), 6366–6484 (FBO) — в области работы.

## 4. Backend (не меняется в задаче; перечислено для полноты)

Маршруты в `app/main.py`: `/api/chats*`, `/api/chat-uploads/*`, `/api/sync/*`, `/api/background/tick`,
`/api/debug/*`, `/api/stats`, `/api/reviews/{id}/start-chat`, `/api/webhooks/yandex`, фоновые циклы
`_background_sync_loop`, `_sync_marketplace_locked`, `_sync_ozon_fast_inbox_locked`, `_message_send_outbox_loop`.
Модули: `app/repository.py`, `app/message_send_models.py`, `app/message_send_operations.py`,
`app/message_send_service.py`, `app/message_outbox_migration.py`, `app/legacy_hosting_upgrade.py`,
`app/task_chat_status_automation.py`, `app/marketplace_sender.py`, `app/connectors/*`, `app/chat_settings_router.py`,
`app/reply_templates_router.py`, `app/schemas.py` (DTO чатов/сообщений).

Глобальные middleware (`security_guard_middleware`, `require_auth_for_api`, `add_fastfox_cache_headers`,
`no_cache_frontend`) затрагивают ответы чатов тоже: любое изменение заголовков кэша/сжатия — только после
отдельного согласования владельцем.

## 5. Тесты-стражи чатов

Эти тесты пинят фрагменты `app.js`/`styles.css`/`index.html` и должны проходить без изменений:
`tests/test_chat_list_date_filter.py`, `test_chat_list_infinite_scroll.py`, `test_chat_operator_ui.py`,
`test_chat_pin_state_ui.py`, `test_chat_read_state_ui.py`, `test_chat_task_visual_contract.py`,
`test_crm_message_author_label_ui.py`, `test_crm_message_identity_ui.py`, `test_message_send_outbox_ui.py`,
`test_reply_templates_ui.py`, `test_notification_day_scope.py`.

## 6. Как проверяется неизменность (этап 6)

1. `git diff 38b34e3 -- app/main.py app/repository.py app/message_send_*.py app/message_outbox_migration.py app/legacy_hosting_upgrade.py app/task_chat_status_automation.py app/connectors app/chat_settings_router.py app/reply_templates_router.py app/schemas.py` — должен быть пустым.
2. Для трёх общих файлов используется скрипт `audit/tools/chat_regions_diff.py` (создаётся на этапе 5): он
   извлекает из `38b34e3` и из рабочего дерева блок `<main id="chatsView">…</main>`, тела функций из
   разделов 2.1 и CSS-правила по селекторам из 3.1–3.2 и сравнивает их побайтно. Вывод прикладывается в
   `audit/05-results.md`.
3. Полный прогон `pytest` с тестами-стражами из раздела 5.
