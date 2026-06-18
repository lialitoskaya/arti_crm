# Основные переменные `.env`

## Общие

```env
MARKETPLACE_BACKGROUND_SYNC=true
MARKETPLACE_BACKGROUND_SYNC_INTERVAL=10
MARKETPLACE_MESSAGE_FETCH_CONCURRENCY=8
```

## Ozon fast inbox

```env
OZON_FAST_INBOX_SYNC_ENABLED=true
OZON_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS=20
OZON_FAST_SYNC_MAX_CHATS=300
OZON_FAST_SYNC_PAGES_PER_VARIANT=3
OZON_FAST_SYNC_VARIANT_MODE=fast
OZON_FAST_HISTORY_PAGES=1
OZON_FAST_MESSAGE_FETCH_CONCURRENCY=10
```

## Ozon safe history policy

```env
OZON_EXCLUDE_SUPPORT_CHATS=0
OZON_DELETE_SUPPORT_CHATS=0
OZON_EXCLUDE_SYSTEM_HISTORY_CHATS=0
OZON_DELETE_SYSTEM_HISTORY_CHATS=0
OZON_MARK_SYSTEM_HISTORY_CHATS=0
```

Смысл: по умолчанию CRM не удаляет и не выбрасывает Ozon-чаты автоматически. Это защищает историю от ошибочной фильтрации.

## Ozon reviews/questions

```env
OZON_REVIEWS_BACKGROUND_SYNC=false
OZON_QUESTIONS_BACKGROUND_SYNC=true
OZON_QUESTIONS_MIN_INTERVAL_SECONDS=300
```

## WB safe mode

```env
WB_BACKGROUND_SYNC=true
WB_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS=3700
WB_RATE_LIMIT_COOLDOWN_SECONDS=3700
WB_FETCH_EVENTS_WITH_CHAT_LIST=false
WB_EVENTS_CURSOR_STATE_FILE=.wb_events_cursor.json
WB_EVENTS_AUTO_IMPORT_ENABLED=true
WB_EVENTS_AUTO_IMPORT_KEEP_ALIVE=true
WB_EVENTS_AUTO_IMPORT_INTERVAL_SECONDS=3700
```

## OpenAI

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
AI_REPLY_STYLE=Вежливо, кратко, по делу...
```

Для production лучше использовать ИИ как черновик ответа, не автосенд.

## Analytics

```env
CRM_ANALYTICS_TZ_OFFSET_MINUTES=180
```

Используется для группировки сообщений по локальным дням и часам. `180` = UTC+3.


## CRM workflow

```env
CRM_AUTO_ASSIGN_FIRST_RESPONSE=true
```

Если включено, первый сотрудник, который успешно ответил в неназначенном чате, автоматически становится ответственным.
