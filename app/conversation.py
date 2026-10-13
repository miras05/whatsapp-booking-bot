"""Сценарий диалога — конечный автомат.

Бот не подтверждает запись сам — он собирает ЗАЯВКУ (услуга, желаемые дата и
время, имя) и отвечает на вопросы по FAQ. Финальное подтверждение и реальное
время — на владелице, она связывается с клиентом сама. Поэтому здесь нет
механики "слотов", гонок за них и откатов — просто дописывание заявки в таблицу.

Состояния:
    START              → приветствие; либо отвечаем на вопрос по FAQ, либо
                         показываем список услуг для заявки
    CHOOSING_SERVICE   → выбрана услуга, спрашиваем удобное время
    ENTERING_TIME      → желаемые дата/время получены (свободным текстом),
                         спрашиваем имя
    ENTERING_NAME      → имя получено, показываем подтверждение
    CONFIRMING         → ждём «да» или «нет»

Правила, на которых держится устойчивость:

1. Любой шаг принимает и нажатие кнопки, и обычный текст с номером варианта.

2. Если на каком-то шаге бот не понял ответ — прежде чем сказать «не понял»,
   он пробует найти вопрос в FAQ. Клиент может в любой момент диалога спросить
   что-то своё, не теряя место в заявке.

3. Если клиент попросил менеджера — бот один раз отвечает и замолкает в этом
   диалоге на HUMAN_TAKEOVER_HOURS. То же самое происходит автоматически, если
   в переписку вступает сама владелица (см. main.py: сообщения, отправленные
   с её телефона через Coexistence, тоже приходят в бот).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app import sheets, whatsapp
from app.config import settings
from app.logging_conf import get_logger
from app.storage import get_session, reset_session, save_session, set_human_active

log = get_logger(__name__)

CANCEL_WORDS = {"отмена", "отменить", "стоп", "stop", "заново", "сначала", "начать заново"}
HELP_WORDS = {"оператор", "человек", "администратор", "менеджер", "help", "помощь"}
YES_WORDS = {"да", "yes", "подтверждаю", "верно", "ок", "окей", "+"}
NO_WORDS = {"нет", "no", "не верно", "неверно", "-"}

MAX_NAME_LENGTH = 60
MAX_PREFERENCE_LENGTH = 200
MIN_FAQ_KEYWORD_LENGTH = 3  # короче — слишком много случайных совпадений


# ---------------------------------------------------------------------------
#  Вспомогательное
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _new_lead_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%y%m%d")
    return f"L-{stamp}-{uuid.uuid4().hex[:6].upper()}"


def _pick(user_text: str, prefix: str, options: list[dict]) -> dict | None:
    """Определяет выбранный вариант: по id из кнопки либо по номеру из текста."""
    raw = (user_text or "").strip()

    if raw.startswith(f"{prefix}:"):
        target = raw.split(":", 1)[1]
        for option in options:
            if str(option["key"]) == target:
                return option
        return None

    digits = re.fullmatch(r"\d{1,3}", raw)
    if digits:
        index = int(raw) - 1
        if 0 <= index < len(options):
            return options[index]
        return None

    normalized = _normalize(raw)
    if normalized:
        for option in options:
            if _normalize(option["label"]) == normalized:
                return option
    return None


def _numbered(options: list[dict]) -> str:
    return "\n".join(f"{i}. {opt['label']}" for i, opt in enumerate(options, start=1))


def _offer(
    wa_id: str,
    options: list[dict],
    prefix: str,
    body: str,
    button_label: str,
) -> None:
    """Показывает варианты списком-кнопкой, при невозможности — нумерованным текстом."""
    pairs = [(f"{prefix}:{opt['key']}", opt["label"]) for opt in options]
    if len(options) <= whatsapp.MAX_LIST_ROWS and whatsapp.send_list(
        wa_id, body, button_label, pairs
    ):
        return
    whatsapp.send_text(wa_id, f"{body}\n\n{_numbered(options)}\n\nОтправьте номер в ответ.")


def _find_faq_answer(text: str) -> str | None:
    """Ищет в FAQ-листе ответ на свободный текст клиента.

    Простое совпадение по ключевым словам, не ИИ — предсказуемо и без
    ежемесячных расходов на LLM. Владелица дополняет список вопросов сама,
    прямо в таблице, без участия разработчика.
    """
    normalized = _normalize(text)
    if len(normalized) < MIN_FAQ_KEYWORD_LENGTH:
        return None

    try:
        faq = sheets.get_faq()
    except Exception as exc:
        log.warning("Не удалось прочитать FAQ: %r", exc)
        return None

    for entry in faq:
        for keyword in entry["keywords"]:
            if keyword in normalized:
                return entry["answer"]
    return None


def _go_to_human(wa_id: str, announce: bool = True) -> None:
    """Передаёт диалог владелице: бот замолкает в нём на HUMAN_TAKEOVER_HOURS."""
    if announce:
        contact = settings.admin_contact or ""
        text = "Хорошо, менеджер скоро свяжется с вами лично."
        if contact and contact != wa_id:
            text += f" Также можно написать напрямую: {contact}"
        whatsapp.send_text(wa_id, text)
    set_human_active(wa_id, settings.human_takeover_hours)
    log.info("Диалог с %s передан владелице.", wa_id)


def _fail(wa_id: str, where: str, exc: Exception) -> None:
    """Единая реакция на непредвиденную ошибку: не молчать и не зациклиться."""
    log.exception("Сбой на шаге %s для %s: %r", where, wa_id, exc)
    contact = f"\n\nСвязаться напрямую: {settings.admin_contact}" if settings.admin_contact else ""
    whatsapp.send_text(
        wa_id,
        "Извините, произошла техническая ошибка. Мы уже знаем о ней."
        f" Попробуйте написать ещё раз чуть позже.{contact}",
    )
    reset_session(wa_id)


# ---------------------------------------------------------------------------
#  Шаги диалога
# ---------------------------------------------------------------------------

def _show_start_menu(wa_id: str, contact_name: str) -> None:
    services = sheets.get_services()
    if not services:
        contact = f" {settings.admin_contact}" if settings.admin_contact else ""
        whatsapp.send_text(
            wa_id,
            "Здравствуйте! Сейчас онлайн-заявка временно недоступна."
            f" Пожалуйста, свяжитесь с администратором.{contact}",
        )
        log.error("Лист «%s» не содержит активных услуг.", settings.sheet_services)
        return

    options = []
    for service in services:
        label = service["name"]
        if service.get("price"):
            label = f"{label} — {service['price']}"
        options.append({"key": service["id"], "label": label, "name": service["name"]})

    greeting = f"Здравствуйте{', ' + contact_name if contact_name else ''}!"
    body = (
        f"{greeting} Это бот «{settings.business_name}».\n"
        "Могу ответить на вопрос или принять заявку на запись.\n\n"
        "Чтобы оставить заявку — выберите услугу:"
    )

    _offer(wa_id, options, "svc", body, "Услуги")
    save_session(wa_id, "CHOOSING_SERVICE", {"options": options})


def _step_start(wa_id: str, text: str, contact_name: str) -> None:
    """Первое сообщение в диалоге: сперва пробуем ответить на вопрос."""
    normalized = _normalize(text)
    if normalized and normalized not in {"здравствуйте", "привет", "добрый день", "здравствуйте!"}:
        answer = _find_faq_answer(text)
        if answer:
            whatsapp.send_text(wa_id, answer)
            whatsapp.send_text(
                wa_id, "Если хотите записаться — просто напишите, я покажу список услуг."
            )
            return

    _show_start_menu(wa_id, contact_name)


def _step_service(wa_id: str, text: str, data: dict) -> None:
    options = data.get("options", [])
    chosen = _pick(text, "svc", options)
    if chosen is None:
        answer = _find_faq_answer(text)
        if answer:
            whatsapp.send_text(wa_id, answer)
            whatsapp.send_text(wa_id, "Продолжим заявку? Выберите услугу из списка выше.")
            return
        whatsapp.send_text(
            wa_id,
            "Не понял выбор. Отправьте номер услуги из списка выше"
            " или напишите «отмена», чтобы начать заново.",
        )
        return

    whatsapp.send_text(
        wa_id,
        f"Услуга: {chosen['name']}\n\nКакие дата и время вам удобны?"
        " Напишите, как удобно, например «завтра после обеда» или «20 августа в 15:00».",
    )
    save_session(wa_id, "ENTERING_TIME", {"service_name": chosen["name"]})


def _step_time(wa_id: str, text: str, data: dict) -> None:
    # FAQ проверяется ДО того, как текст принимается как ответ — иначе вопрос
    # клиента («а вы во сколько открыты?») тихо запишется как «удобное время».
    # Свободный текст почти никогда не бывает "слишком коротким", поэтому без
    # этой проверки первым шагом FAQ здесь вообще не сработал бы.
    answer = _find_faq_answer(text)
    if answer:
        whatsapp.send_text(wa_id, answer)
        whatsapp.send_text(wa_id, "Напишите, какие дата и время вам удобны.")
        return

    preference = re.sub(r"\s+", " ", (text or "").strip())[:MAX_PREFERENCE_LENGTH]
    if len(preference) < 2:
        whatsapp.send_text(wa_id, "Напишите, пожалуйста, желаемые дату и время.")
        return

    whatsapp.send_text(wa_id, "Как вас записать? Напишите, пожалуйста, имя.")
    save_session(wa_id, "ENTERING_NAME", {**data, "preferred_time": preference})


def _step_name(wa_id: str, text: str, data: dict) -> None:
    # Та же причина, что в _step_time: проверяем FAQ раньше, чем примем текст
    # как имя — иначе случайный вопрос посреди диалога станет "именем клиента".
    answer = _find_faq_answer(text)
    if answer:
        whatsapp.send_text(wa_id, answer)
        whatsapp.send_text(wa_id, "Как вас записать? Напишите имя.")
        return

    name = re.sub(r"\s+", " ", (text or "").strip())[:MAX_NAME_LENGTH]
    if len(name) < 2:
        whatsapp.send_text(wa_id, "Пожалуйста, напишите имя — хотя бы две буквы.")
        return

    summary = (
        "Проверьте, всё ли верно:\n\n"
        f"👤 Имя: {name}\n"
        f"💅 Услуга: {data.get('service_name', '')}\n"
        f"🕐 Удобное время: {data.get('preferred_time', '')}\n"
        f"📱 Телефон: +{wa_id}\n\n"
        "Отправить заявку? Менеджер свяжется с вами, чтобы подтвердить точное время."
    )

    if not whatsapp.send_buttons(
        wa_id, summary, [("confirm:yes", "Да, отправить"), ("confirm:no", "Отменить")]
    ):
        whatsapp.send_text(wa_id, f"{summary}\n\nОтветьте «да» или «нет».")

    save_session(wa_id, "CONFIRMING", {**data, "name": name})


def _step_confirm(wa_id: str, text: str, data: dict) -> None:
    raw = (text or "").strip()
    answer_word = _normalize(raw)

    if raw == "confirm:no" or answer_word in NO_WORDS:
        whatsapp.send_text(
            wa_id, "Хорошо, заявка отменена. Напишите в любой момент — начнём заново."
        )
        reset_session(wa_id)
        return

    if raw != "confirm:yes" and answer_word not in YES_WORDS:
        faq_answer = _find_faq_answer(text)
        if faq_answer:
            whatsapp.send_text(wa_id, faq_answer)
            whatsapp.send_text(wa_id, "Подтверждаете заявку? Ответьте «да» или «нет».")
            return
        whatsapp.send_text(wa_id, "Ответьте, пожалуйста, «да» или «нет».")
        return

    lead_id = _new_lead_id()
    lead = {
        "id": lead_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "name": data.get("name", ""),
        "phone": f"+{wa_id}",
        "service": data.get("service_name", ""),
        "preferred_time": data.get("preferred_time", ""),
        "wa_id": wa_id,
    }

    sheets.append_lead(lead)

    whatsapp.send_text(
        wa_id,
        "Спасибо! Заявка принята ✅\n\n"
        f"💅 {lead['service']}\n"
        f"🕐 {lead['preferred_time']}\n"
        f"👤 {lead['name']}\n\n"
        f"Номер заявки: {lead_id}\n"
        "Менеджер свяжется с вами в ближайшее время, чтобы подтвердить точную запись.",
    )
    reset_session(wa_id)
    log.info("Заявка %s оформлена для %s.", lead_id, wa_id)


# ---------------------------------------------------------------------------
#  Точка входа
# ---------------------------------------------------------------------------

_STEPS = {
    "CHOOSING_SERVICE": _step_service,
    "ENTERING_TIME": _step_time,
    "ENTERING_NAME": _step_name,
    "CONFIRMING": _step_confirm,
}


def handle_message(wa_id: str, text: str, contact_name: str = "") -> None:
    """Обрабатывает одно входящее сообщение клиента.

    Вызывается только для сообщений от клиентов. Проверка "не идёт ли уже
    диалог с владелицей" делается снаружи, в main.py, до постановки в очередь.
    """
    normalized = _normalize(text)

    try:
        if normalized in HELP_WORDS:
            _go_to_human(wa_id)
            return

        if normalized in CANCEL_WORDS:
            reset_session(wa_id)
            _step_start(wa_id, "", contact_name)
            return

        state, data = get_session(wa_id)

        if state == "START":
            _step_start(wa_id, text, contact_name)
            return

        step = _STEPS.get(state)
        if step is None:
            # Неизвестное состояние (например, после обновления бота) — не тупик.
            log.warning("Неизвестное состояние %r у %s — начинаю заново.", state, wa_id)
            reset_session(wa_id)
            _step_start(wa_id, text, contact_name)
            return

        step(wa_id, text, data)

    except Exception as exc:
        _fail(wa_id, "handle_message", exc)
