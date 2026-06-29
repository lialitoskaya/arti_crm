Arti CRM v29 image preview no open label fix

Заменить только:
- app/static/app.js
- app/static/styles.css
- app/static/index.html

Если проект использует вложенную папку app/static/static/, заменить также:
- app/static/static/app.js
- app/static/static/styles.css
- app/static/static/index.html

Что изменено:
- убран текстовый блок "Открыть в полном размере" внутри превью изображения;
- изображение остаётся кликабельным и открывается по нажатию;
- v28 с крупным читаемым превью без обрезки сохранён.

Версия статики:
v29-image-preview-no-open-label-20260629
