"""Конфигурация приложения.

Все настройки читаются из переменных окружения (файл .env). Если обязательная
настройка не заполнена, приложение падает СРАЗУ при старте с внятным сообщением,
а не через неделю посреди разговора с клиентом.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Meta / WhatsApp Cloud API ---
    meta_api_base_url: str = "https://graph.facebook.com"
    meta_api_version: str = "v20.0"
    whatsapp_phone_number_id: str
    whatsapp_access_token: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str

    # Номер САМОЙ владелицы в формате без "+", например "77011234567". Через
    # Coexistence её собственные сообщения с телефона приходят в тот же вебхук,
    # что и сообщения клиентов — по этому номеру бот отличает "это она ответила"
    # от "это написал клиент" и замолкает в диалоге, чтобы не отвечать поверх неё.
    whatsapp_business_number: str

    # --- Google Sheets ---
    google_service_account_file: str = "/app/secrets/service-account.json"
    google_spreadsheet_id: str
    sheet_services: str = "Услуги"
    sheet_leads: str = "Заявки"
    sheet_faq: str = "Вопросы"

    # --- Поведение ---
    session_timeout_minutes: int = 30
    max_retries: int = 3
    business_name: str = "Салон красоты"
    admin_contact: str = ""

    # На сколько часов бот замолкает в диалоге с конкретным клиентом после того,
    # как в переписку вступила сама владелица (или бот передал разговор ей).
    human_takeover_hours: int = 24

    # --- Служебное ---
    log_level: str = "INFO"
    log_dir: str = "/app/logs"
    data_dir: str = "/app/data"
    port: int = 8080

    @field_validator("meta_api_version")
    @classmethod
    def _version_looks_sane(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("v"):
            raise ValueError(
                f"META_API_VERSION должна выглядеть как 'v20.0', получено: {v!r}"
            )
        return v

    @field_validator("meta_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def graph_url(self) -> str:
        """Базовый URL Graph API с версией, например https://graph.facebook.com/v20.0"""
        return f"{self.meta_api_base_url}/{self.meta_api_version}"


def load_settings() -> Settings:
    """Читает настройки и превращает ошибку валидации в понятное сообщение."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pragma: no cover - выполняется только при мисконфиге
        raise SystemExit(
            "\n"
            "==================================================================\n"
            " Бот не может стартовать: не заполнены обязательные настройки.\n"
            " Проверьте файл .env (образец — в .env.example).\n"
            "------------------------------------------------------------------\n"
            f"{exc}\n"
            "==================================================================\n"
        ) from exc


settings = load_settings()
