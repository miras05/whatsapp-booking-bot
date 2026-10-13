"""Точка входа: приём вебхуков Meta и фоновая обработка сообщений.

Подключение — через WhatsApp Coexistence: приложение на телефоне владелицы и
Cloud API работают на одном номере одновременно. Это значит, что сюда приходят
ДВА типа сообщений: от клиентов и эхо тех, что владелица отправила сама с
телефона. Второй тип используется только для того, чтобы бот замолкал в этом
диалоге — см. receive_webhook().

Схема работы:

    Meta → POST /webhook → проверка подписи → запись в SQLite → ответ 200 (быстро)
                                                     │
                                          фоновый воркер читает очередь
                                                     │
                                          сценарий диалога → ответ клиенту

Почему так, а не «обработали и ответили»: Meta ждёт ответ несколько секунд и
повторяет доставку, если не дождалась. Обращение к Google Таблице может занять
дольше. Если отвечать только после записи в таблицу, Meta начнёт слать дубли, а
при систематических задержках — вовсе отключит вебхук.

Сообщение попадает в SQLite до ответа 200, поэтому переживает перезапуск
контейнера: после старта воркер доберёт необработанное.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app import sheets, storage, whatsapp
from app.config import settings
from app.conversation import handle_message
from app.logging_conf import get_logger, setup_logging
from app.schemas import WebhookPayload

setup_logging()
log = get_logger(__name__)

WORKER_IDLE_SECONDS = 1.0
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60  # раз в 6 часов
MAX_MESSAGE_ATTEMPTS = 5

_started_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
#  Фоновые задачи
# ---------------------------------------------------------------------------

async def _worker() -> None:
    """Разбирает очередь входящих по одному сообщению за раз.

    Последовательная обработка — сознательный выбор простоты: для салона с
    потоком в десятки сообщений в день запаса производительности более чем
    достаточно, а параллельная обработка добавила бы сложность без реальной
    пользы на таком объёме.
    """
    log.info("Обработчик очереди запущен.")
    while True:
        try:
            pending = await run_in_threadpool(
                storage.next_pending, 10, MAX_MESSAGE_ATTEMPTS
            )

            if not pending:
                await asyncio.sleep(WORKER_IDLE_SECONDS)
                continue

            for row in pending:
                message_id = row["message_id"]
                wa_id = row["wa_id"]
                try:
                    # Владелица уже ведёт этот диалог сама (ответила лично либо
                    # клиент попросил менеджера) — бот молчит, чтобы не отвечать
                    # поверх неё. Сообщение всё равно помечается обработанным:
                    # это сознательный пропуск, а не сбой, повторять не нужно.
                    if await run_in_threadpool(storage.is_human_active, wa_id):
                        log.info("Диалог с %s ведёт владелица — бот пропускает.", wa_id)
                    else:
                        await run_in_threadpool(
                            handle_message, wa_id, row["body"], row["contact_name"]
                        )
                    await run_in_threadpool(storage.mark_processed, message_id)
                except Exception as exc:
                    log.exception("Ошибка обработки %s: %r", message_id, exc)
                    await run_in_threadpool(
                        storage.mark_failed, message_id, repr(exc), MAX_MESSAGE_ATTEMPTS
                    )

        except asyncio.CancelledError:
            log.info("Обработчик очереди остановлен.")
            raise
        except Exception as exc:
            # Воркер не имеет права умереть — иначе бот молча перестанет отвечать.
            log.exception("Непредвиденный сбой в обработчике: %r", exc)
            await asyncio.sleep(5.0)


async def _cleaner() -> None:
    """Периодическая уборка: старые записи журнала, заброшенные сессии и передачи."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            removed_inbox = await run_in_threadpool(storage.purge_old_inbox, 30)
            removed_sessions = await run_in_threadpool(storage.purge_stale_sessions)
            removed_handoffs = await run_in_threadpool(storage.purge_stale_handoffs)
            if removed_inbox or removed_sessions or removed_handoffs:
                log.info(
                    "Уборка: удалено %d записей журнала, %d старых сессий, %d истёкших передач.",
                    removed_inbox, removed_sessions, removed_handoffs,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Сбой уборки: %r", exc)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    storage.init_db()

    # Проверяем доступ к таблице сразу: лучше увидеть проблему в логах при старте,
    # чем узнать о ней от первого клиента.
    if await run_in_threadpool(sheets.healthcheck):
        log.info("Google Таблица доступна.")
    else:
        log.error(
            "ВНИМАНИЕ: Google Таблица недоступна. Бот запустится, но записывать"
            " клиентов не сможет. Проверьте JSON-ключ и доступ к таблице (README, раздел 4)."
        )

    tasks = [asyncio.create_task(_worker()), asyncio.create_task(_cleaner())]
    log.info(
        "Бот запущен. Meta API: %s, таблица: %s",
        settings.graph_url, settings.google_spreadsheet_id,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Бот остановлен.")


app = FastAPI(title="Бот записи в WhatsApp", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
#  Маршруты
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    sheet_ok = await run_in_threadpool(sheets.healthcheck)
    uptime = (datetime.now(timezone.utc) - _started_at).total_seconds()
    return JSONResponse(
        status_code=200 if sheet_ok else 503,
        content={
            "status": "ok" if sheet_ok else "degraded",
            "sheets": "ok" if sheet_ok else "unavailable",
            "uptime_seconds": int(uptime),
            "meta_api_version": settings.meta_api_version,
        },
    )


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Разовая проверка адреса при подключении вебхука в панели Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        log.info("Вебхук подтверждён Meta.")
        return PlainTextResponse(challenge)

    log.warning("Неудачная попытка подтверждения вебхука (mode=%s).", mode)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Принимает входящие сообщения.

    Здесь всегда возвращается 200 — кроме случая неверной подписи. Ошибка нашей
    стороны не должна приводить к бесконечным повторам от Meta и отключению вебхука.
    """
    raw = await request.body()

    if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        return PlainTextResponse("invalid signature", status_code=403)

    try:
        payload = WebhookPayload.model_validate_json(raw)
    except Exception as exc:
        log.warning("Не удалось разобрать вебхук: %r. Тело: %s", exc, raw[:500])
        return PlainTextResponse("ok")

    queued = 0
    for message, contact_name in payload.iter_messages():
        wa_id = message.from_

        # Coexistence: сообщения, которые владелица отправляет сама с телефона,
        # тоже приходят сюда. Отличаем по номеру отправителя и не даём боту
        # обрабатывать их как обращение клиента — вместо этого замолкаем в том
        # диалоге, куда она написала.
        #
        # ТОЧНЫЙ формат этого эха в реальном вебхуке Coexistence не подтверждён
        # (публичной документации с примером JSON найти не удалось) — логируем
        # сырой payload, чтобы на первом живом тесте свериться и поправить при
        # необходимости.
        if wa_id == settings.whatsapp_business_number:
            recipient = message.to
            if recipient:
                await run_in_threadpool(
                    storage.set_human_active, recipient, settings.human_takeover_hours
                )
                log.info("Владелица написала %s сама — бот замолкает в этом диалоге.", recipient)
            else:
                log.warning(
                    "Похоже на эхо сообщения владелицы, но получатель неизвестен"
                    " (пустое поле 'to'). Сырой payload: %s", raw[:1500],
                )
            continue

        body = message.user_text()

        if message.type not in {"text", "interactive"}:
            # Голосовое, фото, стикер — бот их не понимает, но и молчать нельзя.
            log.info("Сообщение %s типа %s — отвечаю подсказкой.", message.id, message.type)
            if await run_in_threadpool(
                storage.enqueue_message, message.id, wa_id, contact_name, ""
            ):
                queued += 1
            continue

        if await run_in_threadpool(
            storage.enqueue_message, message.id, wa_id, contact_name, body
        ):
            queued += 1
            await run_in_threadpool(whatsapp.mark_read, message.id)

    if queued:
        log.info("Принято сообщений в очередь: %d", queued)

    return PlainTextResponse("ok")


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": "whatsapp-booking-bot", "status": "running"})
