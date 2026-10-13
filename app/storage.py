"""Локальное хранилище на SQLite: сессии диалогов и очередь входящих сообщений.

Почему SQLite, а не JSON-файл и не Redis:
  * JSON-файл не переживает одновременную запись и обрыв питания на середине —
    получается битый файл и мёртвый бот;
  * Redis — это ещё один сервис, который надо ставить, обновлять и чинить, что
    противоречит принципу «написал и забыл»;
  * SQLite встроен в Python, атомарен на уровне транзакций и живёт одним файлом,
    который достаточно скопировать, чтобы сделать бэкап.

Две таблицы:
  sessions — на чём остановился каждый клиент в диалоге;
  inbox    — журнал входящих сообщений (он же защита от дублей и от потери
             сообщений при перезапуске контейнера).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
_DB_PATH = os.path.join(settings.data_dir, "bot.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    wa_id       TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
    message_id      TEXT PRIMARY KEY,
    wa_id           TEXT NOT NULL,
    contact_name    TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    received_at     TEXT NOT NULL,
    processed_at    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    -- Раньше сообщение, упавшее с ошибкой, подхватывалось воркером в СЛЕДУЮЩЕЙ
    -- же итерации цикла — при устойчивом сбое (например, Google Sheets лежит
    -- часами) все 5 попыток сгорали за пару секунд, без всякой паузы между
    -- ними. next_attempt_at — минимальное время следующей попытки для ЭТОГО
    -- сообщения, с экспоненциальной паузой. NULL = можно забирать сразу.
    next_attempt_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_pending
    ON inbox (processed_at, attempts, next_attempt_at);

CREATE TABLE IF NOT EXISTS handoff (
    wa_id       TEXT PRIMARY KEY,
    until       TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL переживает внезапное выключение сервера без потери базы.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
    log.info("База состояния готова: %s", _DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
#  Очередь входящих
# --------------------------------------------------------------------------

def enqueue_message(
    message_id: str, wa_id: str, contact_name: str, body: str
) -> bool:
    """Кладёт сообщение в очередь. Возвращает False, если оно уже было.

    Meta повторяет доставку вебхука, если мы не ответили за несколько секунд.
    Первичный ключ по message_id гарантирует, что клиента не запишут дважды.
    """
    with _LOCK, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO inbox (message_id, wa_id, contact_name, body, received_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (message_id, wa_id, contact_name, body, _now()),
            )
            return True
        except sqlite3.IntegrityError:
            log.info("Сообщение %s уже в очереди — повтор от Meta, пропускаю.", message_id)
            return False


def next_pending(limit: int = 10, max_attempts: int = 5) -> list[sqlite3.Row]:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM inbox"
            " WHERE processed_at IS NULL AND attempts < ?"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY received_at ASC LIMIT ?",
            (max_attempts, _now(), limit),
        )
        return cur.fetchall()


def mark_processed(message_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE inbox SET processed_at = ? WHERE message_id = ?",
            (_now(), message_id),
        )


# Пауза перед следующей попыткой, по номеру попытки (1-я неудача → 5 с, и т.д.).
# Растёт быстро: устойчивый сбой (например, Google лежит часами) не должен
# долбить внешние API раз в секунду весь день.
_RETRY_BACKOFF_SECONDS = [5, 15, 60, 300]


def mark_failed(message_id: str, error: str, max_attempts: int = 5) -> None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT attempts FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        attempts_before = row["attempts"] if row else 0
        attempts_after = attempts_before + 1

        if attempts_after >= max_attempts:
            log.error(
                "Сообщение %s: исчерпаны все %d попыток, дальше бот не будет"
                " пытаться сам. Последняя ошибка: %s",
                message_id, max_attempts, error[:300],
            )
            next_attempt = None  # больше не подберёт next_pending (attempts >= max_attempts)
        else:
            backoff_idx = min(attempts_before, len(_RETRY_BACKOFF_SECONDS) - 1)
            pause = _RETRY_BACKOFF_SECONDS[backoff_idx]
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=pause)).isoformat()

        conn.execute(
            "UPDATE inbox SET attempts = ?, last_error = ?, next_attempt_at = ?"
            " WHERE message_id = ?",
            (attempts_after, error[:1000], next_attempt, message_id),
        )


def purge_old_inbox(days: int = 30) -> int:
    """Чистит журнал, чтобы файл базы не рос бесконечно.

    Удаляет и успешно обработанные, и те, что окончательно сдались после всех
    попыток (attempts >= 5) — иначе при затяжном сбое Google/Meta такие строки
    оставались бы в базе навсегда, потому что раньше чистились только
    processed_at IS NOT NULL.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM inbox WHERE received_at < ?"
            " AND (processed_at IS NOT NULL OR attempts >= 5)",
            (cutoff,),
        )
        return cur.rowcount


# --------------------------------------------------------------------------
#  Сессии диалогов
# --------------------------------------------------------------------------

def get_session(wa_id: str) -> tuple[str, dict[str, Any]]:
    """Возвращает (состояние, данные). Просроченные сессии сбрасываются.

    Именно здесь реализован таймаут: если человек начал записываться и пропал,
    через SESSION_TIMEOUT_MINUTES он вернётся к чистому диалогу, а не окажется
    посреди позавчерашнего выбора времени.
    """
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT state, data, updated_at FROM sessions WHERE wa_id = ?", (wa_id,)
        ).fetchone()

    if row is None:
        return "START", {}

    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except ValueError:
        return "START", {}

    age = datetime.now(timezone.utc) - updated
    if age > timedelta(minutes=settings.session_timeout_minutes):
        log.info(
            "Сессия %s протухла (%d мин без активности) — начинаю диалог заново.",
            wa_id, int(age.total_seconds() // 60),
        )
        reset_session(wa_id)
        return "START", {}

    try:
        data = json.loads(row["data"])
    except json.JSONDecodeError:
        log.warning("Повреждены данные сессии %s — сбрасываю.", wa_id)
        return "START", {}

    return row["state"], data


def save_session(wa_id: str, state: str, data: dict[str, Any]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (wa_id, state, data, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(wa_id) DO UPDATE SET"
            " state = excluded.state, data = excluded.data, updated_at = excluded.updated_at",
            (wa_id, state, json.dumps(data, ensure_ascii=False), _now()),
        )


def reset_session(wa_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE wa_id = ?", (wa_id,))


def purge_stale_sessions() -> int:
    """Удаляет давно заброшенные сессии — фоновая уборка."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=settings.session_timeout_minutes * 4)
    ).isoformat()
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


# --------------------------------------------------------------------------
#  Передача диалога человеку (владелице)
# --------------------------------------------------------------------------
#
# Срабатывает в двух случаях: клиент попросил менеджера, либо владелица сама
# ответила в этом чате с телефона (через Coexistence её исходящие тоже видны
# боту). Пока диалог "передан", бот в нём молчит — иначе будет накладка, когда
# бот отвечает поверх её сообщения.

def set_human_active(wa_id: str, hours: int) -> None:
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO handoff (wa_id, until) VALUES (?, ?)"
            " ON CONFLICT(wa_id) DO UPDATE SET until = excluded.until",
            (wa_id, until),
        )
    # Старое состояние диалога больше не актуально — человек ведёт разговор сам.
    reset_session(wa_id)


def is_human_active(wa_id: str) -> bool:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT until FROM handoff WHERE wa_id = ?", (wa_id,)
        ).fetchone()
    if row is None:
        return False
    try:
        until = datetime.fromisoformat(row["until"])
    except ValueError:
        return False
    return datetime.now(timezone.utc) < until


def purge_stale_handoffs() -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM handoff WHERE until < ?", (now,))
        return cur.rowcount
