# Переключение с SQLite на PostgreSQL

Перенос выполняется только после проверки GitHub Actions и при временно
остановленном боте, чтобы новые заявки не попали после снимка SQLite. Сайт и бот
до отдельного подключения PostgreSQL продолжают использовать текущую базу.

## Подготовка

1. Подготовьте пустую PostgreSQL-базу и отдельный пароль пользователя.
2. Задайте на VPS переменные `POSTGRES_PASSWORD`, `TARGET_DATABASE_URL` и
   `DATABASE_URL`. URL должен использовать `postgresql+asyncpg://`; если пароль
   содержит специальные символы, URL-кодируйте его.
3. Не удаляйте SQLite volume или файл. FSM бота продолжит использовать
   `/data/bot_database.db` через `DATABASE_PATH`.

## Перенос и проверка

Остановите бота, чтобы во время копирования не менялась исходная база. Затем
запустите только PostgreSQL и убедитесь, что контейнер здоров:

```bash
docker compose stop bot
docker compose -f compose.yaml -f compose.web.yaml --profile web up -d postgres
```

Затем создайте точную резервную копию SQLite через встроенный backup API,
скопируйте строки в пустую PostgreSQL и проверьте количества таблиц:

```bash
docker compose -f compose.yaml -f compose.web.yaml --profile web run --rm --no-deps \
  -e SOURCE_SQLITE_PATH=/data/bot_database.db \
  bot python -m repair_bot.scripts.migrate_sqlite_to_postgres
```

В `.env` для этой команды задайте `TARGET_DATABASE_URL`. Скрипт остановится, если
целевые таблицы уже содержат записи, обнаружит несверившееся число строк или
неизвестные таблицы. В JSON-результате отображаются только путь резервной копии,
названия таблиц и количество строк. Исходная SQLite не изменяется.

Перед переключением убедитесь, что все числа строк совпали и бэкап-файл
существует. После этого задайте в `DATABASE_URL` тот же PostgreSQL URL и
запускайте стек с профилем сайта. При проблеме верните `DATABASE_URL` на SQLite;
не удаляйте volume PostgreSQL и снимок.

```bash
docker compose -f compose.yaml -f compose.web.yaml --profile web up -d --build
```

Для внешнего доступа сайт слушает только `127.0.0.1:8000`; настройка домена и
HTTPS reverse proxy выполняется отдельно. Production требует уникальный
`WEB_SESSION_SECRET` и `WEB_COOKIE_SECURE=1`.

## Резервное копирование

После переключения установите `ops/systemd/courseworkbot-backup.service` и
`ops/systemd/courseworkbot-backup.timer` в `/etc/systemd/system/`, затем выполните:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now courseworkbot-backup.timer
```

Таймер сохраняет PostgreSQL dump и снимок SQLite FSM раз в 14 дней, оставляя три
последние пары в `/var/backups/courseworkbot`. Они находятся на том же VPS, как
было согласовано, и не защищают от потери всего сервера.
