"""Сценарий диалога: от приветствия до заявки в таблице."""

from __future__ import annotations

from app import conversation, storage

CLIENT = "77011112233"


def test_full_booking_flow(chat, wa, leads):
    assert "выберите услугу" in chat("привет")
    assert "Какие дата и время" in chat("svc:1")
    assert "Как вас записать" in chat("завтра после обеда")
    assert "Проверьте, всё ли верно" in chat("Айгерим")
    assert "Заявка принята" in chat("confirm:yes")

    assert len(leads) == 1
    lead = leads[0]
    assert lead["service"] == "Чистка лица"
    assert lead["preferred_time"] == "завтра после обеда"
    assert lead["name"] == "Айгерим"
    assert lead["phone"] == f"+{CLIENT}"
    assert lead["id"].startswith("L-")
    assert storage.get_session(CLIENT) == ("START", {})


def test_service_can_be_chosen_by_number_or_label(chat):
    chat("привет")
    assert "Пилинг" in chat("2")

    chat("отмена")
    assert "Чистка лица" in chat("чистка лица — 15 000 ₸")


def test_unknown_choice_keeps_state(chat):
    chat("привет")
    assert "Не понял выбор" in chat("99")
    assert storage.get_session(CLIENT)[0] == "CHOOSING_SERVICE"


def test_faq_question_mid_flow_is_not_taken_as_answer(chat, wa, leads):
    """Вопрос посреди заявки не должен записаться как «удобное время»."""
    chat("привет")
    chat("svc:1")
    chat("а во сколько вы работаете?")

    assert "Работаем с 10:00 до 20:00." in wa.texts()
    state, data = storage.get_session(CLIENT)
    assert state == "ENTERING_TIME"
    assert "preferred_time" not in data


def test_faq_answered_on_first_message(chat, wa):
    chat("подскажите адрес")
    assert "Мы на Абая, 1." in wa.texts()
    assert storage.get_session(CLIENT)[0] == "START"


def test_decline_on_confirmation(chat, leads):
    chat("привет")
    chat("svc:2")
    chat("в субботу")
    chat("Дана")
    assert "заявка отменена" in chat("нет")
    assert leads == []


def test_help_word_hands_dialog_to_owner(chat):
    assert "менеджер скоро свяжется" in chat("оператор")
    assert storage.is_human_active(CLIENT)


def test_unexpected_error_does_not_leave_client_hanging(chat, wa, monkeypatch):
    def broken():
        raise RuntimeError("Google недоступен")

    monkeypatch.setattr(conversation.sheets, "get_services", broken)
    assert "техническая ошибка" in chat("привет")
    assert storage.get_session(CLIENT) == ("START", {})


def test_pick_helper():
    options = [{"key": "a", "label": "Первое"}, {"key": "b", "label": "Второе"}]
    assert conversation._pick("svc:b", "svc", options)["key"] == "b"
    assert conversation._pick("1", "svc", options)["key"] == "a"
    assert conversation._pick("  второе ", "svc", options)["key"] == "b"
    assert conversation._pick("3", "svc", options) is None
    assert conversation._pick("svc:zzz", "svc", options) is None
