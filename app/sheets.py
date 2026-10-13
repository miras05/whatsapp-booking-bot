"""Работа с Google Таблицей через Sheets API v4.

Бот не занимает записи сам — он собирает заявку (услуга, желаемые дата/время, имя),
а подтверждает и вносит в расписание сама владелица. Поэтому здесь нет механики
"слотов" и гонок за них: обычное дописывание строк, без перепроверок и откатов.

Ключевые решения ради устойчивости:

1. Колонки ищутся ПО НАЗВАНИЮ заголовка, а не по номеру. Администратор может
   вставить колонку в середину, поменять их местами или дописать свои — бот
   продолжит работать.

2. FAQ читается заново при каждом вопросе клиента — если владелица дописала
   новый вопрос в таблицу, бот подхватит его без перезапуска.

3. Пустые строки и лишние пробелы в ячейках обрабатываются молча.
"""

from __future__ import annotations

import threading
from typing import Any

import google.auth.transport.requests
import httpx
from google.oauth2 import service_account

from app.config import settings
from app.logging_conf import get_logger
from app.retry import PermanentError, with_retries

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

_creds_lock = threading.Lock()
_credentials: service_account.Credentials | None = None


class SheetError(Exception):
    """Проблема со структурой таблицы — её должен исправить администратор."""


# ---------------------------------------------------------------------------
#  Авторизация
# ---------------------------------------------------------------------------

def _get_credentials() -> service_account.Credentials:
    """Сервисный аккаунт: токен обновляется сам, вручную ничего продлевать не нужно."""
    global _credentials
    with _creds_lock:
        if _credentials is None:
            try:
                _credentials = service_account.Credentials.from_service_account_file(
                    settings.google_service_account_file, scopes=SCOPES
                )
            except FileNotFoundError as exc:
                raise PermanentError(
                    f"Не найден JSON-ключ сервисного аккаунта: "
                    f"{settings.google_service_account_file}. См. README, раздел 4."
                ) from exc
            except ValueError as exc:
                raise PermanentError(
                    f"JSON-ключ сервисного аккаунта повреждён или имеет неверный формат: {exc}"
                ) from exc

        if not _credentials.valid:
            _credentials.refresh(google.auth.transport.requests.Request())
        return _credentials


def _headers() -> dict[str, str]:
    creds = _get_credentials()
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
#  Низкоуровневые вызовы
# ---------------------------------------------------------------------------

def _read_range(a1_range: str) -> list[list[str]]:
    def call() -> list[list[str]]:
        url = f"{SHEETS_API}/{settings.google_spreadsheet_id}/values/{a1_range}"
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=_headers(), params={"majorDimension": "ROWS"})
            resp.raise_for_status()
            return resp.json().get("values", [])

    return with_retries(call, what=f"чтение {a1_range}", max_attempts=settings.max_retries)


def _append_row(sheet_name: str, values: list[Any]) -> None:
    def call() -> None:
        url = f"{SHEETS_API}/{settings.google_spreadsheet_id}/values/{sheet_name}!A1:append"
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                headers=_headers(),
                params={
                    "valueInputOption": "USER_ENTERED",
                    "insertDataOption": "INSERT_ROWS",
                },
                json={"majorDimension": "ROWS", "values": [values]},
            )
            resp.raise_for_status()

    with_retries(call, what=f"добавление строки в {sheet_name}", max_attempts=settings.max_retries)


# ---------------------------------------------------------------------------
#  Таблица как объект: заголовки + строки
# ---------------------------------------------------------------------------

class Table:
    """Прочитанный лист: заголовки приведены к нижнему регистру без пробелов."""

    def __init__(self, sheet_name: str, raw: list[list[str]]):
        self.sheet_name = sheet_name
        if not raw:
            raise SheetError(
                f"Лист «{sheet_name}» пустой. Нужна хотя бы строка заголовков."
            )
        self._headers = [str(h).strip().lower() for h in raw[0]]
        self._rows: list[tuple[int, list[str]]] = []
        for offset, row in enumerate(raw[1:], start=2):  # start=2: строка 1 — заголовки
            if any(str(cell).strip() for cell in row):
                self._rows.append((offset, list(row)))

    def column_index(self, *names: str) -> int:
        """Находит колонку по одному из допустимых названий."""
        for name in names:
            target = name.strip().lower()
            if target in self._headers:
                return self._headers.index(target)
        raise SheetError(
            f"В листе «{self.sheet_name}» нет колонки {' / '.join(names)}. "
            f"Найдены колонки: {', '.join(self._headers) or '(пусто)'}"
        )

    def rows(self):
        """Отдаёт (номер_строки_в_таблице, значение_по_названию_колонки)."""
        for row_number, cells in self._rows:
            record = {}
            for idx, header in enumerate(self._headers):
                record[header] = str(cells[idx]).strip() if idx < len(cells) else ""
            yield row_number, record


def _load(sheet_name: str) -> Table:
    return Table(sheet_name, _read_range(f"{sheet_name}!A1:ZZ"))


# ---------------------------------------------------------------------------
#  Прикладные операции
# ---------------------------------------------------------------------------

def get_services() -> list[dict[str, str]]:
    """Список активных услуг из листа «Услуги».

    Ожидаемые колонки: ID, Название, Длительность, Цена, Активна.
    Обязательны только ID и Название — остальные необязательны.
    """
    table = _load(settings.sheet_services)
    id_col = table.column_index("id", "ид")
    name_col = table.column_index("название", "услуга", "наименование")

    headers_lower = table._headers
    has_active = "активна" in headers_lower or "активно" in headers_lower

    services: list[dict[str, str]] = []
    for _, record in table.rows():
        service_id = record.get(headers_lower[id_col], "")
        name = record.get(headers_lower[name_col], "")
        if not service_id or not name:
            continue
        if has_active:
            flag = (record.get("активна") or record.get("активно") or "").lower()
            if flag in {"нет", "no", "false", "0", "-"}:
                continue
        services.append(
            {
                "id": service_id,
                "name": name,
                "price": record.get("цена", ""),
                "duration": record.get("длительность", ""),
            }
        )
    return services


def append_lead(lead: dict[str, str]) -> None:
    """Дописывает заявку в лист «Заявки».

    Это НЕ подтверждённая запись — владелица сама связывается с клиентом и
    подтверждает время. Поэтому здесь нет проверки занятости, нет отката: просто
    дописывается строка, и всё.
    """
    table = _load(settings.sheet_leads)

    # Синонимы: администратор мог назвать колонки по-своему.
    #
    # Дата и время удобства клиента — ОДНО поле (бот задаёт один вопрос "какие
    # дата и время вам удобны?" и получает один свободный ответ вроде "завтра
    # после обеда"). Отдельной колонки "Желаемая дата" в схеме нет намеренно —
    # заранее делить один ответ клиента на дату и время отдельно бот не умеет.
    mapping = {
        "id": lead["id"],
        "id заявки": lead["id"],
        "дата создания": lead["created_at"],
        "создано": lead["created_at"],
        "имя": lead["name"],
        "клиент": lead["name"],
        "телефон": lead["phone"],
        "услуга": lead["service"],
        "желаемое время": lead.get("preferred_time", ""),
        "желаемая дата/время": lead.get("preferred_time", ""),
        "когда удобно": lead.get("preferred_time", ""),
        "время": lead.get("preferred_time", ""),
        "статус": "Новая заявка",
        "whatsapp id": lead["wa_id"],
        "wa_id": lead["wa_id"],
        "комментарий": lead.get("comment", ""),
    }

    row = [mapping.get(header, "") for header in table._headers]

    if not any(row):
        raise SheetError(
            f"В листе «{settings.sheet_leads}» не найдено ни одной знакомой колонки. "
            f"Ожидаются: ID, Дата создания, Имя, Телефон, Услуга, Желаемая дата, "
            f"Желаемое время, Статус. Сейчас там: {', '.join(table._headers)}"
        )

    _append_row(settings.sheet_leads, row)
    log.info("Заявка %s добавлена в таблицу.", lead["id"])


def get_faq() -> list[dict[str, str]]:
    """Список вопрос-ответ из листа «Вопросы».

    Ожидаемые колонки: Ключевые слова (через запятую), Ответ. Если листа нет —
    бот просто не отвечает на вопросы, но не падает (не все клиенты захотят
    заводить этот лист сразу).
    """
    try:
        table = _load(settings.sheet_faq)
    except SheetError:
        return []

    try:
        kw_col = table.column_index("ключевые слова", "вопрос", "ключевые фразы")
        answer_col = table.column_index("ответ")
    except SheetError as exc:
        log.warning("Лист «%s» есть, но не хватает колонок: %s", settings.sheet_faq, exc)
        return []

    headers_lower = table._headers
    entries: list[dict[str, str]] = []
    for _, record in table.rows():
        keywords_raw = record.get(headers_lower[kw_col], "")
        answer = record.get(headers_lower[answer_col], "")
        if not keywords_raw or not answer:
            continue
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
        if keywords:
            entries.append({"keywords": keywords, "answer": answer})
    return entries


def healthcheck() -> bool:
    """Проверка доступности таблицы — вызывается при старте и в /health."""
    try:
        _read_range(f"{settings.sheet_services}!A1:A1")
        return True
    except Exception as exc:
        log.error("Таблица недоступна: %r", exc)
        return False
