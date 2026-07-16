# Вклад в CRM

CRM поддерживается как долгоживущая production-система. Любое изменение должно быть
небольшим, проверяемым и ограниченным одной задачей.

## Ветка и scope

1. Не работайте напрямую в `main`.
2. Обновите базовую ветку и создайте от неё отдельную рабочую ветку.
3. Одна задача — одна логическая ветка. Не расширяйте согласованный scope по ходу
   работы.
4. Используйте один из префиксов: `fix/`, `feat/`, `test/`, `refactor/`, `style/`,
   `docs/`, `chore/`, `security/`.
5. Имя ветки должно быть коротким и отражать единственную цель, например
   `fix/message-deduplication`.

## Commits

Сообщения оформляются в стиле Conventional Commits:

```text
fix(sync): prevent duplicate message import
docs(repo): clarify review workflow
```

Один commit должен иметь одну основную цель. Не смешивайте функцию с рефакторингом,
bugfix с массовым форматированием или несвязанными визуальными изменениями. Не
используйте сообщения `update`, `fixes`, `changes`, `final`, `new version` и
`various improvements`.

## Проверки

Перед commit выполните все доступные и релевантные проверки:

- syntax/compile checks для изменённых Python- и JavaScript-файлов;
- backend- и целевые regression tests, если соответствующая suite существует;
- `git diff --check` и проверку merge-маркеров;
- проверку diff на секреты, runtime/user data и случайное форматирование;
- schema/migration checks при изменениях БД;
- ручную проверку desktop и mobile при изменениях UI.

Нельзя утверждать, что проверка прошла, если она не запускалась. Недоступную или не
настроенную проверку явно укажите в pull request вместе с причиной.

## Данные и безопасность

Не коммитьте `.env`, credentials, ключи, cookies, токены, production dumps,
runtime SQLite, caches, probes, uploads, пользовательские вложения и локальную
инфраструктуру инструментов. Test fixtures должны быть обезличены и храниться в
`tests/fixtures/`. Наличие трёх исторических fixture-файлов в `chat_attachments/`
не разрешает добавлять туда новые данные.

## Pull request

Перед открытием PR:

1. Просмотрите полный substantive diff, name-status и stat.
2. Проверьте чистоту worktree после commit.
3. Получите независимый review для изменения.
4. Сразу отправьте рабочую ветку в `origin` и откройте PR.
5. Опишите цель, границы scope, фактически выполненные проверки, риски, миграции и
   rollback.

После merge удалите обычную рабочую ветку. Protected, baseline и backup-ветки не
удаляйте без отдельного решения владельца.

Подробный процесс описан в
[`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md).
