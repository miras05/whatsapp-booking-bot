# Конкретная версия образа, а не «latest»: пересборка через год не должна
# внезапно поднять Python другой версии и сломать работающего бота.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Almaty

WORKDIR /app

# curl нужен для проверки здоровья контейнера.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Каталоги для базы состояния и логов — локально монтируются через volume
# (docker-compose), на Fly.io /app/data держится на постоянном fly volume,
# остальное — на эфемерном диске контейнера.
RUN mkdir -p /app/data /app/logs /app/secrets

# Процесс работает не от root: если приложение когда-нибудь скомпрометируют,
# ущерб будет ограничен.
RUN useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]

# Один воркер — принципиально. Обработка сообщений последовательная — для
# салона с потоком в десятки сообщений в день этого более чем достаточно, а
# параллельная обработка добавила бы сложность без реальной пользы.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
