"""Логирование с ротацией файлов.

Диск сервера не переполнится: максимум 10 файлов по 5 МБ = 50 МБ, дальше старые
удаляются автоматически. Логи дублируются в stdout, чтобы их было видно через
`docker compose logs`.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from app.config import settings

_CONFIGURED = False

MAX_BYTES = 5 * 1024 * 1024  # 5 МБ на файл
BACKUP_COUNT = 10  # + 10 архивных файлов


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    os.makedirs(settings.log_dir, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        os.path.join(settings.log_dir, "bot.log"),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # httpx на INFO печатает каждый запрос — слишком шумно для многолетней работы.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
