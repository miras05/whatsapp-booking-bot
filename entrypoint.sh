#!/bin/sh
# Точка входа контейнера.
#
# Локально (docker-compose) JSON-ключ Google монтируется файлом через volume —
# он просто уже лежит на месте, ничего делать не нужно.
#
# На Fly.io файлы так не смонтируешь — секреты там только переменные
# окружения. Поэтому ключ передаётся в base64 через
# GOOGLE_SERVICE_ACCOUNT_JSON_B64, а этот скрипт перед стартом раскладывает
# его в обычный файл по тому же пути, что ждёт приложение. Код sheets.py
# ничего об этом не знает — он как читал файл с диска, так и читает.
set -e

if [ -n "$GOOGLE_SERVICE_ACCOUNT_JSON_B64" ] && [ ! -f "$GOOGLE_SERVICE_ACCOUNT_FILE" ]; then
    mkdir -p "$(dirname "$GOOGLE_SERVICE_ACCOUNT_FILE")"
    echo "$GOOGLE_SERVICE_ACCOUNT_JSON_B64" | base64 -d > "$GOOGLE_SERVICE_ACCOUNT_FILE"
fi

exec "$@"
