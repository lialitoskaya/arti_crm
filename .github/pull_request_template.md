# Цель

<!-- Какую одну задачу решает PR и какое ожидаемое поведение? -->

## Что изменено

<!-- Краткий список substantive changes. -->

## Что намеренно не изменено

<!-- Зафиксируйте границы scope и отложенные задачи. -->

## Связанная задача

<!-- Issue, task или согласованный запрос. -->

## Проверки

<!-- Укажите точные команды и фактические результаты. Не отмечайте незапущенное как passed. -->

- [ ] Выполнены релевантные syntax/compile checks
- [ ] Выполнены backend/targeted regression tests либо ниже объяснено, почему они недоступны
- [ ] Выполнен `git diff --check`
- [ ] Проверены merge markers, secrets и runtime/user data

Недоступные проверки и причина:

<!-- Название проверки, причина, компенсирующая ручная проверка. -->

## Ручная проверка

<!-- Сценарии и результаты; для UI отдельно desktop и mobile. -->

## Миграции

<!-- Нет / план применения / повторный запуск / rollback или recovery. -->

## Security и PII

<!-- Влияние на auth/session/CSRF, secrets, logs и пользовательские данные. -->

## UI/UX gate

<!-- Для UI обязательно свериться с docs/UI_DESIGN_STANDARDS.md. Для не-UI: не применимо. -->

- [ ] Прочитан и применён `docs/UI_DESIGN_STANDARDS.md`
- [ ] Решение сохраняет существующую дизайн-систему и плотность CRM
- [ ] Устранена первопричина; старый конкурирующий UI/CSS/state-путь удалён
- [ ] Нет необоснованных AI-slop паттернов: glassmorphism, glow, generic AI-gradients, лишних карточек и pill-кнопок
- [ ] Нет duplicate selectors, handlers, DOM IDs, design tokens и sources of truth
- [ ] Проверены desktop, mobile, длинный контент, overflow и touch targets
- [ ] Проверены loading, empty, error, disabled, read-only, keyboard и focus-visible
- [ ] Проверены DOM-size и отсутствие performance-регрессии

## Screenshots

<!-- Для UI: before/after на desktop и mobile. Для не-UI: не применимо. -->

## Rollback

<!-- Как безопасно отменить изменение или восстановить совместимость? -->

## Reviewer verdict

<!-- Reviewer, verdict и подтверждение отсутствия blocker/high замечаний. -->

## Обязательный checklist

- [ ] Scope не расширен
- [ ] В diff нет секретов и credentials
- [ ] В diff нет runtime- и пользовательских данных
- [ ] Нет случайного массового форматирования
- [ ] Указанные tests действительно запускались
- [ ] Документация обновлена, если это необходимо
- [ ] Миграции и rollback описаны, если это необходимо
- [ ] Независимый reviewer не оставил blocker/high замечаний
- [ ] Worktree/index чисты после commit
