v67 — WB events cursor import

Что видно по debug:
- Список WB-чats работает: local_total_in_db=100.
- lastMessage и время уже восстановлены: messages_count=1, has_last_message_in_metadata=true.
- WB /seller/events вернул 50 событий и response_next.
- Но токен имеет X-Ratelimit-Limit=1, поэтому вторую страницу сразу получать нельзя.

Что исправлено:
- /api/debug/wb/import-events теперь сохраняет response_next в .wb_events_cursor.json.
- Следующий запуск после cooldown продолжит историю с сохранённого next, а не начнёт снова с первой страницы.
- Это позволяет постепенно выгрузить историю WB: 1 страница за 1 cooldown.
- /api/debug/version теперь показывает реальную app.version, а не старую 0.54.0.

Как пользоваться:
1. Ждать, пока /api/debug/wb покажет cooldown_remaining_seconds=0.
2. Открыть /api/debug/wb/import-events ровно один раз.
3. Если saved_next_for_next_run заполнен — следующий запуск после следующего cooldown продолжит старую историю.
4. Если нужно начать заново с самых свежих событий:
   /api/debug/wb/import-events?reset=true

Важно:
- Быстрее, чем разрешает WB X-Ratelimit-Limit=1, полную историю получить нельзя.
- Список чатов и lastMessage продолжают работать без events.
