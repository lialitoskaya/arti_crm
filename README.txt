Arti CRM v28 readable image preview fix

Заменить только:
- app/static/app.js
- app/static/styles.css
- app/static/index.html

Если проект использует вложенную папку app/static/static/, заменить также:
- app/static/static/app.js
- app/static/static/styles.css
- app/static/static/index.html

Что изменено:
- сообщения с изображениями получили отдельный класс message-has-images;
- пузырь сообщения с изображением стал шире;
- превью изображения стало крупнее;
- изображение больше не обрезается: используется object-fit: contain;
- сохранено читаемое превью на мобильной и десктопной версии;
- v27 визуал сообщений сохранён.

Версия статики:
v28-readable-image-preview-20260629
