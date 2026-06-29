# Arti CRM v20 backend sync fix

## Основание

`/api/sync/operator` возвращал ошибку Ozon:

```text
UNIQUE constraint failed: messages.chat_id, messages.external_message_id
```

Это означает, что backend пытался повторно вставить marketplace-сообщение с уже существующим `external_message_id` в том же `chat_id`. Из-за этого падала вся Ozon-синхронизация, и новые сообщения не доходили до БД.

## Root cause

В проекте есть несколько источников sync:

- background sync loop;
- frontend operator sync `/api/sync/operator`;
- manual/debug sync endpoints.

Они могли пересекаться по времени. Даже если `repo.add_message()` делал предварительный SELECT, между SELECT и INSERT другой sync мог уже вставить то же сообщение. Итог — SQLite UNIQUE constraint.

## Изменения

### app/repository.py

`add_message()` получил жёсткую идемпотентность:

- ранний exact lookup по `(chat_id, external_message_id)`;
- update existing row вместо падения;
- `INSERT OR IGNORE` вместо обычного `INSERT`;
- если insert был проигнорирован из-за race condition, выполняется повторный select/update и возвращается существующий message_id.

### app/main.py

Добавлены marketplace-specific locks:

- `ozon`;
- `yandex`;
- `wildberries`.

Теперь background sync и frontend sync не должны одновременно выполнять импорт одного и того же marketplace.

Также снижены concurrency defaults:

- Ozon fast sync: 4;
- Yandex: 3, максимум 4;
- WB: 2.

## Что проверено

- Python compile OK.
- Project checker: Python compile OK.
- Frontend JS: node --check OK.
- CSS parse OK.

## Что смотреть после установки

1. `/api/sync/operator` больше не должен показывать Ozon `UNIQUE constraint failed`.
2. Если Ozon status стал `ok` или `partial_error` без UNIQUE, значит sync больше не падает на дублях.
3. Новые сообщения должны появиться в БД и отобразиться в интерфейсе через v19 live-refresh.
