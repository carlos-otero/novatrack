"""Environment-only runtime configuration; never load an implicit .env file."""

from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False, extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )
    database_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    app_base_url: str
    secret_key: SecretStr
    track17_api_key: SecretStr
    tz: str = "UTC"
    log_level: str = "INFO"
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_thread_id: int | None = None
    trusted_proxy_ips: str = "127.0.0.1"
    allowed_oidc_subjects: str = ""
    worker_poll_seconds: int = 5

    @field_validator("app_base_url", "oidc_issuer")
    @classmethod
    def https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A canonical HTTPS URL without credentials/query/fragment is required")
        return value.rstrip("/")

    @field_validator("secret_key")
    @classmethod
    def strong_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("SECRET_KEY must have at least 32 bytes")
        return value

    @field_validator("tz")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("worker_poll_seconds")
    @classmethod
    def positive_poll(cls, value: int) -> int:
        if not 1 <= value <= 300:
            raise ValueError("WORKER_POLL_SECONDS must be between 1 and 300")
        return value
