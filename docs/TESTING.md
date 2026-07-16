# Тестирование CRM

## Назначение

Regression foundation фиксирует существующее поведение перед дальнейшим
рефакторингом. Тесты не меняют production-код, не используют реальные credentials,
пользовательские данные, production SQLite или API маркетплейсов.

## Установка

Test dependencies отделены от production dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

Не добавляйте pytest в `requirements.txt`: этот файл предназначен для runtime
приложения.

## Запуск

Основная команда:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Cache provider отключён, поэтому запуск не создаёт `.pytest_cache/`. Текущий набор
также совместим со standard library runner, если runtime dependencies уже
установлены:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Нельзя сообщать об успешном результате команды, которая фактически не запускалась.

## Границы безопасности

Безопасный bootstrap выполняется до первого `import app.*`:

- environment процесса заменяется минимальным test-only набором;
- `dotenv.load_dotenv` подменяется no-op, поэтому `.env` не читается;
- `DATABASE_PATH`, attachments и state/cache paths направляются во временный
  каталог;
- SQLite guard разрешает подключение только к конкретной временной БД;
- временные SQLite/WAL/SHM-файлы удаляются после каждого теста;
- socket guard блокирует внешние соединения;
- HTTP smoke requests используют только in-memory `httpx.ASGITransport`;
- FastAPI startup/lifespan не запускается;
- fixtures синтетические и обезличенные;
- legacy-файлы из `chat_attachments/` не используются.

Если тест пытается открыть другую SQLite или выполнить network connection, он
завершается ошибкой.

## Текущее покрытие

Набор `tests/test_regression_foundation.py` фиксирует:

1. import приложения в изолированном environment без создания БД;
2. offline smoke behavior для `/health`, `/` и
   `/static/manifest.webmanifest`;
3. прямую viewer/admin границу `_require_admin`;
4. последовательную идемпотентность `repository.add_message` для одинакового
   external message ID: одна строка, тот же ID и обновлённый preview.

## Осознанные ограничения

Foundation пока не проверяет:

- FastAPI startup/lifespan, migrations, cleanup/repair jobs и background tasks;
- полную login/session middleware chain;
- конкурентную доставку одинакового external message ID — принятый baseline не
  содержит DB unique constraint для этого инварианта;
- contract behavior Ozon, Wildberries и Yandex;
- реальные HTTP, marketplace credentials и production data;
- binary fixtures.

Если новый тест требует изменения production-кода, вынесите такое изменение в
отдельную задачу и отдельный commit.
