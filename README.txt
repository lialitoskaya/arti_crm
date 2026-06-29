Arti CRM v30 mobile extra actions fix

Заменить только:
- app/static/app.js
- app/static/styles.css
- app/static/index.html

Если проект использует вложенную папку app/static/static/, заменить также:
- app/static/static/app.js
- app/static/static/styles.css
- app/static/static/index.html

Что исправлено:
- пункты меню открытого чата в мобильной версии снова открывают свои панели;
- кнопки с data-extra теперь обрабатываются напрямую через pointerdown;
- делегированный обработчик через conversation больше не перехватывает эти пункты;
- добавлена защита от synthetic click после pointerdown на iOS Safari;
- внешний pointerdown больше не закрывает панель при нажатии на пункт меню.

Сохранено:
- v25 ускорение мобильной версии;
- v26 switcher-логика меню;
- v27 оформление сообщений;
- v28/v29 крупные превью изображений без лишней подписи.

Версия статики:
v30-mobile-extra-actions-fix-20260629
