v60 — исправление Ozon Questions: отправка ответа требует SKU

Проблема:
- После исправления question_id Ozon начал возвращать:
  invalid QuestionAnswerCreateRequest.Sku: value must be greater than 0
- Это значит, что /v1/question/answer/create требует передавать не только question_id и текст ответа, но и sku товара.

Исправлено:
- При отправке ответа CRM теперь передаёт sku в /v1/question/answer/create.
- SKU берётся из локальной карточки вопроса и raw_json Ozon: sku, product_sku, product.sku, product_info.sku, sku_info.sku и вложенных полей.
- Если SKU не найден, CRM покажет понятную локальную ошибку без пустого запроса в Ozon.
- Обновлена версия: v60_ozon_question_answer_sku_fix_2026-06-15.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Открыть /api/debug/version — должна быть v60_ozon_question_answer_sku_fix_2026-06-15.
5. В разделе «Вопросы» нажать «Обновить».
6. Открыть вопрос заново и отправить ответ.
