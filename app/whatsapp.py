"""Клиент WhatsApp Cloud API.

Никаких сторонних SDK: только документированные HTTP-вызовы Graph API. Если
завтра автор популярной обёртки забросит свою библиотеку, нас это не коснётся.

Исходящие сообщения собираются строго по схеме Meta — в отличие от входящих,
где мы, наоборот, терпимы к неизвестным полям.
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.retry import with_retries

log = get_logger(__name__)

# Ограничения Meta, зашитые в протокол. Превышение = ошибка 400.
MAX_TEXT_LENGTH = 4096
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
MAX_BUTTON_TITLE = 20
MAX_BODY_LENGTH = 1024


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Проверяет, что запрос действительно от Meta, а не от постороннего.

    Без этой проверки любой, кто узнает адрес вебхука, сможет слать боту
    поддельные сообщения и создавать записи от чужого имени.
    """
    if not signature_header:
        log.warning("Запрос без заголовка подписи — отклоняю.")
        return False

    if not signature_header.startswith("sha256="):
        log.warning("Неизвестный формат подписи: %s", signature_header[:32])
        return False

    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    provided = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        log.warning("Подпись не совпала — запрос отклонён.")
        return False

    return True


def _post(payload: dict) -> None:
    url = f"{settings.graph_url}/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    def call() -> None:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()

    with_retries(call, what="отправка сообщения в WhatsApp", max_attempts=settings.max_retries)


def _chunk(text: str) -> list[str]:
    """Режет слишком длинный текст по границам строк."""
    if len(text) <= MAX_TEXT_LENGTH:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_TEXT_LENGTH:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def send_text(to: str, text: str) -> None:
    for part in _chunk(text):
        _post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": part},
            }
        )


def send_list(
    to: str,
    body: str,
    button_label: str,
    options: list[tuple[str, str]],
    header: str = "",
) -> bool:
    """Отправляет список выбора. options = [(id, подпись), ...].

    Возвращает False, если вариантов больше, чем допускает Meta (10) — тогда
    вызывающий код показывает обычный нумерованный список текстом.
    """
    if not options or len(options) > MAX_LIST_ROWS:
        return False

    rows = []
    for option_id, title in options:
        rows.append(
            {
                "id": option_id[:200],
                "title": title[:MAX_ROW_TITLE],
            }
        )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:MAX_BODY_LENGTH]},
            "action": {
                "button": button_label[:MAX_BUTTON_TITLE],
                "sections": [{"title": "Выберите", "rows": rows}],
            },
        },
    }
    if header:
        payload["interactive"]["header"] = {"type": "text", "text": header[:60]}

    try:
        _post(payload)
        return True
    except Exception as exc:
        # Интерактивные сообщения — удобство, а не необходимость. Если Meta их
        # почему-то не принимает, диалог должен продолжиться обычным текстом.
        log.warning("Не удалось отправить список, откатываюсь на текст: %r", exc)
        return False


def send_buttons(to: str, body: str, options: list[tuple[str, str]]) -> bool:
    """До трёх кнопок. options = [(id, подпись), ...]."""
    if not options or len(options) > 3:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:MAX_BODY_LENGTH]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": oid[:200], "title": title[:MAX_BUTTON_TITLE]},
                    }
                    for oid, title in options
                ]
            },
        },
    }

    try:
        _post(payload)
        return True
    except Exception as exc:
        log.warning("Не удалось отправить кнопки, откатываюсь на текст: %r", exc)
        return False


def mark_read(message_id: str) -> None:
    """Ставит галочку «прочитано» — клиент видит, что его услышали."""
    url = f"{settings.graph_url}/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                url,
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
    except Exception as exc:
        # Косметика: не смогли отметить прочитанным — не повод прерывать работу.
        log.debug("Не удалось отметить %s прочитанным: %r", message_id, exc)
