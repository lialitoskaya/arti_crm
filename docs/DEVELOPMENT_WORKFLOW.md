# Git и процесс разработки CRM

## Назначение

Этот документ определяет обязательный процесс для небольших безопасных изменений
CRM. Приоритеты: корректность, целостность данных, безопасность, тестируемость и
понятные границы ответственности.

`main` и принятые baseline-ветки используются как стабильные точки интеграции.
Разработка напрямую в них запрещена. Каждая задача выполняется в отдельной ветке и
доставляется через pull request.

## Процесс одной задачи

1. **Обновить базовую ветку.** Получить актуальные refs и убедиться, что выбрана
   согласованная база. Не переписывать принятую историю.
2. **Создать отдельную ветку.** Одна задача — одна логическая ветка.
3. **Зафиксировать scope.** Записать ожидаемое поведение, инварианты, разрешённые
   файлы и то, что намеренно не меняется.
4. **Сначала определить проверки.** Для изменения поведения добавить или обновить
   characterization/regression tests, когда это допускает задача и test suite.
5. **Внести минимальное изменение.** Исправлять самую раннюю неправильную точку,
   не расширяя задачу соседним рефакторингом.
6. **Просмотреть diff.** Проверить name-status, stat и полный substantive diff.
7. **Выполнить обязательные проверки.** Использовать применимый набор из раздела
   [Проверки](#проверки).
8. **Провести независимый review.** Автор изменения не может быть единственным,
   кто его одобрил. Blocker/high замечания исправляются до commit.
9. **Создать логический commit.** Commit содержит одну основную цель и сообщение в
   стиле Conventional Commits.
10. **Сразу отправить ветку в `origin`.** После commit проверить чистоту дерева и
    выполнить push рабочей ветки.
11. **Создать pull request.** Заполнить шаблон фактическими результатами, не
    выдавая незапущенные проверки за успешные.
12. **Удалить рабочую ветку после merge.** Это относится к обычной task-ветке.
    Protected, baseline и backup-ветки удаляются только по отдельному решению
    владельца.

После commit и push не начинать следующую задачу в той же ветке.

## Имена веток

Разрешённые префиксы:

| Префикс | Назначение | Пример |
| --- | --- | --- |
| `fix/` | Исправление дефекта | `fix/message-deduplication` |
| `feat/` | Новая функция | `feat/order-note` |
| `test/` | Тесты без изменения production-поведения | `test/sync-retry` |
| `refactor/` | Изменение структуры без новой функции | `refactor/chat-service` |
| `style/` | Изолированная визуальная правка | `style/mobile-chat-header` |
| `docs/` | Документация | `docs/git-development-workflow` |
| `chore/` | Обслуживание репозитория | `chore/update-fixtures-policy` |
| `security/` | Изолированное security-изменение | `security/session-csrf` |

Имя после префикса должно быть коротким, написанным в kebab-case и отражать одну
задачу. Названия вроде `fix/everything` или `feat/new-version` не задают проверяемой
границы и не используются.

## Сообщения commits

Формат:

```text
<type>(<scope>): <краткая цель>
```

Допустимые основные types соответствуют префиксам веток: `fix`, `feat`, `test`,
`refactor`, `style`, `docs`, `chore`, `security`.

Примеры:

```text
fix(sync): serialize marketplace imports
test(messages): cover repeated delivery
refactor(chats): extract message rendering
style(chats): align mobile action menu
docs(repo): define Git and development workflow
```

Сообщение описывает результат в повелительной форме и одну основную цель.
Запрещены неинформативные сообщения: `update`, `fixes`, `changes`, `final`,
`new version`, `various improvements`.

## Границы commits

Не смешивать в одном commit:

- новую функцию и самостоятельный рефакторинг;
- backend-изменение и несвязанный visual polish;
- bugfix и глобальное форматирование;
- DB migration и несвязанный UI;
- test fixtures и пользовательские данные;
- upgrade зависимостей и изменение продукта, если upgrade не является самой
  задачей.

Backend, frontend и CSS могут находиться в одном commit только тогда, когда вместе
образуют одну завершённую вертикальную функцию и не содержат побочных улучшений.
Formatting-only изменения отделяются от semantic changes.

## Проверки

Набор зависит от изменённых файлов и риска. Минимальный gate включает:

1. **Python syntax.** Для изменённых модулей выполнить, например:

   ```powershell
   python -m py_compile app\main.py
   ```

   Если в проектном окружении команда `python` недоступна, использовать
   согласованный Python launcher.
2. **Backend tests.** Запустить существующую test suite и целевые regression tests.
   В текущем baseline общая tracked test suite не настроена; до её появления это
   ограничение явно указывается в PR. Нельзя писать «tests passed», если suite не
   запускалась.
3. **Frontend syntax.** Проверить каждый изменённый JavaScript-файл доступным
   parser/runtime, если он установлен. В baseline нет закреплённого Node toolchain,
   поэтому отсутствие такой проверки явно фиксируется в PR.
4. **Targeted regression.** Для bugfix тест должен воспроизводить старую ошибку и
   подтверждать исправление. Для retry/sync проверить повтор, timeout, reordered
   events, partial failure и конкурентный запуск, когда это применимо.
5. **Whitespace:**

   ```powershell
   git diff --check
   ```

6. **Merge markers.** Проверить изменённые tracked-файлы на `<<<<<<<`, `=======` и
   `>>>>>>>`; документированные примеры не считать конфликтом без контекста.
7. **Secrets.** Проверить staged diff на `.env` contents, credentials, private
   keys, tokens, cookies, authorization headers и персональные данные. Названия
   запрещённых сущностей в policy-документации сами по себе не являются секретами.
8. **Runtime и user data.** Убедиться, что в index нет SQLite, dumps, probes,
   caches, logs, uploads, пользовательских вложений и локальной инфраструктуры
   инструментов.
9. **Schema/migration.** При изменении БД проверить upgrade на временной local/test
   БД, повторный запуск, совместимость данных и recovery/rollback.
10. **Manual UI.** При изменении frontend отдельно пройти desktop и mobile
    сценарии, включая navigation, loading/error/empty states и повторные действия.

Lint, format, typecheck, integration и E2E выполняются, когда соответствующие
инструменты реально настроены в репозитории. Если обязательная для риска проверка
недоступна, PR должен назвать её, причину и компенсирующую ручную проверку. Никогда
не указывать успешный результат для незапущенной команды.

## Независимый review

Reviewer сверяет задачу с полным diff, а не только с описанием автора, и проверяет:

- одна ли у ветки и commit основная цель;
- не расширен ли scope;
- находится ли исправление на корректной архитектурной границе;
- защищены ли data integrity, idempotency и transaction boundaries;
- достаточны ли tests и честно ли записаны их результаты;
- нет ли секретов, runtime/user data, generated files и случайного форматирования;
- обновлена ли необходимая документация;
- предусмотрены ли migration и rollback, если они нужны.

До commit не должно оставаться blocker/high замечаний. Reviewer не исправляет diff
молча: замечания возвращаются автору, проверки после исправлений повторяются.

## Рефакторинг

1. Сначала зафиксировать внешнее поведение characterization/regression tests.
2. Явно указать сохраняемые контракты и архитектурную границу.
3. Работать небольшими вертикальными срезами.
4. Не добавлять новую функцию в refactor commit.
5. Не менять соседние модули только ради единообразия.
6. Новый путь вводить до миграции ограниченных consumers; старый путь удалять лишь
   после доказательства, что он больше не используется.
7. Временный compatibility path должен иметь условие и план удаления.

Широкое переписывание без characterization tests и проверяемого migration path не
принимается.

## Изменения базы данных

- Schema changes должны быть явными, reviewable и сопровождаться migration plan.
- Запрещено молча изменять production DB при старте приложения без описанного
  rollout/recovery решения.
- Migration должна быть повторяемой или безопасно определять уже применённое
  состояние.
- Для destructive migration до выполнения нужны backup, rollback или forward
  recovery и отдельное подтверждение владельца.
- Tests работают только с временной local/test БД и никогда с production DB.
- Runtime SQLite и его служебные файлы не коммитятся.
- Для внешних сущностей задаётся устойчивая identity strategy; конкурентная
  уникальность защищается на уровне БД, когда это возможно.

## Frontend и CSS

- Разделять API transport, server state, local UI state, rendering и handlers.
- Не добавлять без необходимости новые globals.
- Не дублировать selectors, event handlers и конкурирующие источники состояния.
- Использовать общие design tokens вместо повторяющихся magic values.
- Не наращивать цепочки override-правил; старые CSS-блоки удалять или объединять
  только после проверки эквивалентного поведения.
- Desktop и mobile проверять отдельно.
- Optimistic updates должны иметь reconciliation и rollback.
- Новые вложенные production-копии в `app/static/static/` запрещены; рабочий
  frontend хранится непосредственно в `app/static/`.

## Интеграции маркетплейсов

- Ozon, Wildberries и Yandex имеют отдельные integration boundaries и mapping.
- Marketplace HTTP, retry и payload mapping не размещаются в routes, UI или
  repository.
- Явно определить timeout, rate limit, retry/backoff, error classes и
  idempotency strategy.
- Tests используют mocks или обезличенные replay fixtures и не вызывают реальные
  marketplace API.
- Проверять повторную доставку, retry после timeout, reordered events, partial
  failure и конкурентный sync.
- Tokens, Client-Id, cookies, authorization headers и сырые приватные payload не
  попадают в Git и test output.

## Безопасность и данные

Запрещено коммитить:

- `.env`, credentials, private keys, tokens, cookies и session data;
- production dumps, runtime SQLite, caches, probes и logs;
- uploads, сырые клиентские данные и пользовательские вложения;
- локальные каталоги и конфигурацию Codex/IDE/agent tooling.

Изменения CSRF, session, authentication и authorization сопровождаются security
tests и явным описанием угрозы. Logs не должны содержать tokens, cookies, полные
headers, адреса, телефоны, PII и сырые сообщения.

Fixtures должны быть обезличены и храниться в `tests/fixtures/`. Три существующих
legacy fixture-файла в `chat_attachments/` являются закрытым исключением baseline,
а не разрешением добавлять новые файлы или считать весь каталог безопасным.

## Git safety

Без отдельного решения владельца запрещено:

- force push в `main`;
- rebase уже опубликованной общей ветки;
- удаление protected, baseline и backup-веток;
- переписывание принятой истории.

Всегда запрещено:

- `git reset --hard` и `git clean -fd` как обычный рабочий процесс;
- `git add .` без предварительного просмотра всех путей;
- commit незавершённого или расширенного scope;
- обход независимого reviewer;
- смешивание локальной Codex/IDE/agent-инфраструктуры с проектным diff;
- перезапись чужих незакоммиченных изменений.

Merge, rebase, cherry-pick, revert и удаление refs выполняются только когда они
явно входят в согласованную задачу.

## Gate перед commit

Перед staging и commit зафиксировать:

1. текущую ветку и ожидаемый parent;
2. `git status --short`;
3. полный name-status;
4. stat;
5. substantive diff;
6. фактические результаты tests и проверок;
7. forbidden scan для secrets, runtime/user data и локальной инфраструктуры;
8. независимый reviewer verdict.

Stage выполняется явным перечислением разрешённых paths. После staging повторно
проверяются cached name-status, stat, diff, `git diff --cached --check` и отсутствие
unstaged residue.

## После commit

1. Проверить, что worktree/index чисты.
2. Показать hash, parent, name-status и stat созданного commit.
3. Сразу выполнить push рабочей ветки в `origin`, при первом push установив
   upstream.
4. Убедиться, что remote-tracking ref указывает на созданный commit.
5. Открыть PR и остановиться до следующей отдельной задачи.

Следующий commit не начинается автоматически в завершённой ветке.
