# Настройка Web Push для Arti CRM

## 1. Установить зависимость

```bash
pip install -r requirements_push.txt
```

## 2. Создать VAPID-ключи

Самый простой вариант через pywebpush/py-vapid, установленный вместе с зависимостью:

```bash
python -m py_vapid --gen
```

Если команда отличается в вашем окружении, можно сгенерировать VAPID-ключи любым стандартным Web Push/VAPID generator и вставить public/private key в `.env`.

## 3. Добавить в .env

```env
WEB_PUSH_ENABLED=1
WEB_PUSH_VAPID_PUBLIC_KEY=...
WEB_PUSH_VAPID_PRIVATE_KEY=...
WEB_PUSH_VAPID_SUBJECT=mailto:artitechno.official@gmail.com
CRM_PUBLIC_BASE_URL=https://ваш-домен
WEB_PUSH_OUTBOX_INTERVAL_SECONDS=3
```

## 4. Перезапустить приложение

Нужно перезапустить FastAPI-процесс, потому что изменились `app/main.py`, `app/db.py`, `app/repository.py` и env.

## 5. Подключить устройство

### Desktop

1. Открыть CRM в браузере.
2. Нажать «Уведомления».
3. Разрешить уведомления.
4. Проверить `/api/push/status` и `/api/push/test`.

### iPhone

1. Открыть CRM в Safari.
2. Добавить сайт на экран Домой.
3. Открыть CRM с иконки.
4. Нажать «Уведомления».
5. Разрешить уведомления.
6. Проверить `/api/push/test`.

## Что теперь происходит

Открытая CRM больше не является источником уведомления. Сервер сам:

1. опрашивает маркетплейсы;
2. создаёт запись в `notifications`;
3. кладёт событие в `push_outbox`;
4. отправляет push на все активные подписки пользователя.

Если приложение закрыто, desktop/iPhone всё равно смогут получить push, пока сервер CRM работает.

Версия: v47-real-web-push-desktop-mobile-20260630
