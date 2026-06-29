Arti CRM v35 desktop chat menu position fix — single static

Это НЕ полный проект. Не распаковывать поверх всей папки проекта.

Заменить только:
- app/static/styles.css
- app/static/index.html

Двойной папки app/static/static здесь больше нет.

Что исправлено:
- меню ⋯ в открытом чате на десктопе привязано к .chat-controls;
- добавлены position: relative / overflow: visible / z-index;
- текущее расположение элементов в верхней плашке сохранено.
