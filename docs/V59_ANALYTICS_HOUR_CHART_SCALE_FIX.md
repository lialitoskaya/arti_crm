Arti CRM v59 analytics hourly chart scale fix

Это НЕ полный проект. Не распаковывать поверх всей папки проекта.

Заменить только:
- app/static/app.js
- app/static/styles.css
- app/static/index.html

Что исправлено:
- диаграмма по часам в аналитике теперь строится по реальным значениям из app.js;
- убраны старые CSS pseudo-bars с hardcoded nth-child значениями, из-за которых 13:00 мог выглядеть как максимум;
- часы со значением 0 теперь имеют нулевую заливку, а не минимальную полоску;
- в тёмной теме у analytics-hour-row / analytics-hour-button нет фоновой карточки;
- строка остаётся прозрачной и в обычном состоянии, и при hover.

Проверки:
- node --check app.js OK
- CSS braces OK

Версия:
v59-analytics-hour-chart-correct-scale-20260701
