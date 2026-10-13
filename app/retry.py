"""Повторные попытки с экспоненциальной задержкой.

Зачем: и Google, и Meta периодически отвечают 429 (слишком часто) или 5xx
(у них проблемы). Это нормальная жизнь, а не авария — почти всегда через
несколько секунд всё работает. Без ретраев такие моменты выглядели бы для
клиента как «бот сломался».

Задержка растёт: 1с → 2с → 4с, плюс случайная добавка (jitter), чтобы при
массовом сбое все клиенты не долбились в API одновременно.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from app.logging_conf import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Коды, при которых имеет смысл повторить попытку.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0


class RetryableError(Exception):
    """Временная ошибка — есть смысл повторить."""


class PermanentError(Exception):
    """Постоянная ошибка — повторять бессмысленно (неверный ключ, нет доступа и т.п.)."""


def _delay_for(attempt: int) -> float:
    raw = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    capped = min(raw, MAX_DELAY_SECONDS)
    return capped + random.uniform(0, capped * 0.25)


def classify_http_error(exc: httpx.HTTPStatusError) -> Exception:
    """Решает, временная это ошибка или окончательная."""
    status = exc.response.status_code
    body = exc.response.text[:500]
    if status in RETRYABLE_STATUS:
        return RetryableError(f"HTTP {status}: {body}")
    return PermanentError(f"HTTP {status}: {body}")


def with_retries(
    func: Callable[[], T],
    *,
    what: str,
    max_attempts: int,
) -> T:
    """Выполняет func, повторяя при временных ошибках.

    Сетевые таймауты и обрывы соединения считаются временными автоматически.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except httpx.HTTPStatusError as exc:
            err = classify_http_error(exc)
            if isinstance(err, PermanentError):
                log.error("%s: окончательная ошибка, повторять не буду. %s", what, err)
                raise err from exc
            last_error = err
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = RetryableError(f"сеть: {exc!r}")
        except PermanentError:
            raise
        except Exception as exc:  # неизвестная ошибка — считаем временной один раз
            last_error = RetryableError(f"неожиданно: {exc!r}")

        if attempt < max_attempts:
            pause = _delay_for(attempt)
            log.warning(
                "%s: попытка %d/%d не удалась (%s). Повтор через %.1f с.",
                what, attempt, max_attempts, last_error, pause,
            )
            time.sleep(pause)
        else:
            log.error(
                "%s: исчерпаны все %d попытки. Последняя ошибка: %s",
                what, max_attempts, last_error,
            )

    raise last_error or RetryableError(f"{what}: не удалось выполнить")
