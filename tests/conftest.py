"""Общие фикстуры.

Настройки читаются при импорте app.config, поэтому окружение задаётся здесь,
до первого импорта приложения. Внешние сервисы (Meta, Google) в тестах не
вызываются: whatsapp.* и sheets.* подменяются фейками, которые запоминают вызовы.
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="booking-bot-tests-")

os.environ.update(
    {
        "WHATSAPP_PHONE_NUMBER_ID": "100000000000000",
        "WHATSAPP_ACCESS_TOKEN": "test-token",
        "WHATSAPP_VERIFY_TOKEN": "verify-me",
        "WHATSAPP_APP_SECRET": "app-secret",
        "WHATSAPP_BUSINESS_NUMBER": "77000000000",
        "GOOGLE_SPREADSHEET_ID": "test-sheet",
        "ADMIN_CONTACT": "+7 700 000 00 00",
        "DATA_DIR": os.path.join(_TMP, "data"),
        "LOG_DIR": os.path.join(_TMP, "logs"),
    }
)

import pytest  # noqa: E402

from app import conversation, sheets, storage, whatsapp  # noqa: E402

storage.init_db()

SERVICES = [
    {"id": "1", "name": "Чистка лица", "price": "15 000 ₸"},
    {"id": "2", "name": "Пилинг", "price": ""},
]
FAQ = [
    {"keywords": ["адрес", "где вы"], "answer": "Мы на Абая, 1."},
    {"keywords": ["во сколько", "график"], "answer": "Работаем с 10:00 до 20:00."},
]


class FakeWhatsApp:
    """Запоминает всё, что бот «отправил» клиенту."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to: str, text: str) -> None:
        self.sent.append((to, text))

    def send_list(self, to, body, button_label, options, header="") -> bool:
        self.sent.append((to, body))
        return True

    def send_buttons(self, to, body, options) -> bool:
        self.sent.append((to, body))
        return True

    def mark_read(self, message_id: str) -> None:
        pass

    @property
    def last(self) -> str:
        return self.sent[-1][1]

    def texts(self) -> str:
        return "\n".join(text for _, text in self.sent)


@pytest.fixture(autouse=True)
def clean_db():
    with storage._connect() as conn:
        conn.executescript("DELETE FROM sessions; DELETE FROM inbox; DELETE FROM handoff;")
    yield


@pytest.fixture
def wa(monkeypatch) -> FakeWhatsApp:
    fake = FakeWhatsApp()
    for name in ("send_text", "send_list", "send_buttons", "mark_read"):
        monkeypatch.setattr(whatsapp, name, getattr(fake, name))
    return fake


@pytest.fixture
def leads(monkeypatch) -> list[dict]:
    """Подменяет Google Таблицу: услуги и FAQ — из констант, заявки — в список."""
    saved: list[dict] = []
    monkeypatch.setattr(sheets, "get_services", lambda: SERVICES)
    monkeypatch.setattr(sheets, "get_faq", lambda: FAQ)
    monkeypatch.setattr(sheets, "append_lead", saved.append)
    monkeypatch.setattr(sheets, "healthcheck", lambda: True)
    return saved


@pytest.fixture
def chat(wa, leads):
    """Короткий способ написать боту от имени клиента."""

    def say(text: str, wa_id: str = "77011112233") -> str:
        conversation.handle_message(wa_id, text, "Айгерим")
        return wa.last

    return say
