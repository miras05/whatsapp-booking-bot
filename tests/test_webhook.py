"""HTTP-слой: подтверждение вебхука, проверка подписи, постановка в очередь."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import storage, whatsapp
from app.main import app
from app.schemas import WebhookPayload

SECRET = b"app-secret"


def _signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": f"sha256={signature}", "Content-Type": "application/json"}


def _payload(message: dict, name: str = "Аня") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": message["from"], "profile": {"name": name}}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture
def client(wa, leads):
    # Без контекстного менеджера lifespan не запускается — фоновый воркер в
    # тестах не нужен, проверяем только приём вебхука.
    return TestClient(app)


def test_verify_webhook(client):
    ok = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
    )
    assert ok.status_code == 200 and ok.text == "42"

    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope"})
    assert bad.status_code == 403


def test_rejects_bad_signature(client):
    raw = json.dumps(_payload({"id": "w1", "from": "7701", "type": "text"})).encode()
    resp = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 403
    assert storage.next_pending() == []


def test_text_message_is_queued_once(client):
    body = _payload({"id": "w2", "from": "7701", "type": "text", "text": {"body": "привет"}})
    raw, headers = _signed(body)

    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200  # повтор Meta

    pending = storage.next_pending()
    assert len(pending) == 1
    assert pending[0]["body"] == "привет"
    assert pending[0]["contact_name"] == "Аня"


def test_owner_echo_silences_bot_in_that_chat(client):
    """Владелица ответила сама с телефона — бот замолкает в этом диалоге."""
    body = _payload(
        {"id": "w3", "from": "77000000000", "to": "7709", "type": "text", "text": {"body": "Здравствуйте!"}}
    )
    raw, headers = _signed(body)
    client.post("/webhook", content=raw, headers=headers)

    assert storage.is_human_active("7709")
    assert storage.next_pending() == []


def test_garbage_body_still_returns_200(client):
    raw = b"not json"
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    resp = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": f"sha256={signature}"})
    assert resp.status_code == 200


def test_signature_helper():
    raw = b"{}"
    good = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(raw, good)
    assert not whatsapp.verify_signature(raw, None)
    assert not whatsapp.verify_signature(raw, "md5=abc")


def test_button_reply_is_read_as_its_id():
    payload = WebhookPayload.model_validate(
        _payload(
            {
                "id": "w4",
                "from": "7701",
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "confirm:yes", "title": "Да"}},
            }
        )
    )
    message, name = next(payload.iter_messages())
    assert message.user_text() == "confirm:yes"
    assert name == "Аня"


def test_long_text_is_chunked():
    text = "\n".join(["x" * 100] * 100)  # ~10 000 символов
    parts = whatsapp._chunk(text)
    assert len(parts) == 3
    assert all(len(p) <= whatsapp.MAX_TEXT_LENGTH for p in parts)
