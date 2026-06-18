# Code audit v84

## Проверено

- Python-файлы компилируются.
- Явная ошибка `NameError: os is not defined` в `repository.py` исправлена ранее.
- В v84 дополнительно исправлена опасная логика Ozon system-history deletion: по умолчанию CRM больше не удаляет Ozon-чаты автоматически.
- Ozon fast sync и deep backfill разделены.

## Основные проблемы структуры

### 1. `app/main.py` слишком большой

Сейчас около 3000 строк. В нём смешаны:
- API routes;
- background sync loop;
- Ozon/WB debug endpoints;
- AI reply;
- reviews/questions;
- helper-функции.

Риск: каждое изменение может случайно задеть несвязанную часть.

Рекомендация:
- вынести routes в `app/api/*`;
- sync в `app/services/*`;
- debug endpoints в `app/api/debug.py`.

### 2. `app/repository.py` слишком большой

Сейчас это единый repository на все сущности:
- chats;
- messages;
- users;
- tasks;
- reviews;
- questions;
- knowledge.

Рекомендация:
- разделить на `repositories/chats.py`, `repositories/messages.py`, `repositories/reviews.py`, `repositories/questions.py`.

### 3. Ozon connector перегружен

`app/connectors/ozon.py` содержит:
- chats;
- messages;
- reviews;
- questions;
- diagnostics;
- normalization.

Рекомендация:
- `connectors/ozon/chats.py`;
- `connectors/ozon/reviews.py`;
- `connectors/ozon/questions.py`;
- общий `client.py`.

### 4. Frontend монолитный

`index.html`, `app.js`, `styles.css` стали большими.

Рекомендация:
- сначала разделить CSS по секциям комментариями;
- затем вынести JS-модули: chats, reviews, questions, knowledge, settings.
- если проект растёт — перейти на Vite/React/Vue/Svelte, но не срочно.

## Что не стоит делать резко

Не стоит сразу переписывать всё на новую архитектуру. Без тестов это рискованно.

Лучший путь:
1. Зафиксировать текущее поведение.
2. Добавить smoke checks.
3. Выносить модули по одному.
4. После каждого шага проверять sync Ozon/WB и UI.

## Приоритеты следующего технического этапа

1. Пагинация списка чатов.
2. Пагинация сообщений внутри чата.
3. Индексы под большие объёмы истории.
4. Раздел `Система / Синхронизация` вместо debug URL.
5. Классификация Ozon service/customer/unknown без удаления.
6. Автотесты для repository и connector normalizers.

## Изменения v85

- Добавлен первый сервисный модуль `app/services/analytics.py`.
- Новый раздел аналитики реализован через отдельный backend service, чтобы не увеличивать `main.py` сильнее.
- Добавлены индексы SQLite для сообщений/чатов, полезные при больших объёмах истории.
- Добавлен `docs/ANALYTICS.md`.

Технический долг остаётся: frontend пока монолитный, но новый раздел отделён логически и документирован.


## v86 analytics

Система подсчёта обращений изменена: обращение = первое входящее сообщение клиента в чате за локальный день. Все зависимые показатели аналитики — общий счётчик, пиковые часы, outside-hours и среднее время ответа — переведены на эту модель.
