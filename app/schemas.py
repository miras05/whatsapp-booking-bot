"""Схемы входящих вебхуков Meta.

Принцип: `extra="ignore"`. Meta регулярно добавляет в вебхуки новые поля — если
валидировать строго «ничего лишнего», бот однажды утром сломается сам по себе.
Поэтому неизвестные поля молча игнорируются, а обязательным считается только тот
минимум, без которого сообщение бессмысленно.

Обратная сторона: то, что мы ОТПРАВЛЯЕМ, наоборот собирается строго — см. whatsapp.py.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TextBody(_Lenient):
    body: str = ""


class InteractiveListReply(_Lenient):
    id: str = ""
    title: str = ""


class InteractiveButtonReply(_Lenient):
    id: str = ""
    title: str = ""


class Interactive(_Lenient):
    type: str = ""
    list_reply: InteractiveListReply | None = None
    button_reply: InteractiveButtonReply | None = None


class InboundMessage(_Lenient):
    id: str
    from_: str = Field(alias="from")
    timestamp: str = ""
    type: str = "unknown"
    text: TextBody | None = None
    interactive: Interactive | None = None
    # Поле "to" в обычных вебхуках Cloud API отсутствует (сообщение всегда для
    # номера бизнеса). При Coexistence эхо собственных сообщений владелицы
    # МОЖЕТ его содержать — берём на всякий случай, чтобы не потерять адресата.
    # Точный формат эха уточняется на первом живом тесте, см. main.py.
    to: str = ""

    def user_text(self) -> str:
        """Достаёт текст ответа независимо от того, нажал человек кнопку или написал руками."""
        if self.interactive is not None:
            if self.interactive.list_reply is not None:
                return self.interactive.list_reply.id or self.interactive.list_reply.title
            if self.interactive.button_reply is not None:
                return (
                    self.interactive.button_reply.id
                    or self.interactive.button_reply.title
                )
        if self.text is not None:
            return self.text.body
        return ""


class Profile(_Lenient):
    name: str = ""


class Contact(_Lenient):
    wa_id: str = ""
    profile: Profile | None = None


class ChangeValue(_Lenient):
    messaging_product: str = ""
    contacts: list[Contact] = []
    messages: list[InboundMessage] = []
    # Статусы доставки (sent/delivered/read) приходят сюда же. Нам они не нужны,
    # но их наличие не должно ничего ломать.
    statuses: list[dict[str, Any]] = []


class Change(_Lenient):
    field: str = ""
    value: ChangeValue | None = None


class Entry(_Lenient):
    id: str = ""
    changes: list[Change] = []


class WebhookPayload(_Lenient):
    object: str = ""
    entry: list[Entry] = []

    def iter_messages(self):
        """Разворачивает вложенную структуру Meta в плоский список сообщений."""
        for entry in self.entry:
            for change in entry.changes:
                if change.value is None:
                    continue
                contact_name = ""
                if change.value.contacts:
                    profile = change.value.contacts[0].profile
                    contact_name = profile.name if profile else ""
                for message in change.value.messages:
                    yield message, contact_name
