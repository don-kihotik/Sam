from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = Field(default="", repr=False)
    telegram_allowed_chat_id: int | None = None
    alexey_telegram_user_id: int | None = None
    andrey_telegram_user_id: int | None = None
    telegram_enabled: bool = True

    openai_api_key: str = Field(default="", repr=False)
    database_url: str = Field(default="sqlite+aiosqlite:///./sam.sqlite3", repr=False)

    sam_model: str = "gpt-5.4-mini"
    extraction_model: str = "gpt-5.4-mini"
    transcription_model: str = "gpt-4o-mini-transcribe"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embeddings_enabled: bool = True

    timezone: str = "America/Toronto"
    debug: bool = False
    recent_message_limit: int = Field(default=24, ge=5, le=100)
    video_max_frames: int = Field(default=12, ge=4, le=24)
    policy_max_text_chars: int = Field(default=6000, ge=500, le=12000)
    policy_hourly_requests: int = Field(default=30, ge=1, le=300)
    policy_daily_requests: int = Field(default=120, ge=1, le=1000)
    policy_global_daily_requests: int = Field(default=300, ge=1, le=3000)
    policy_audio_max_seconds: int = Field(default=180, ge=10, le=600)
    policy_video_max_seconds: int = Field(default=300, ge=10, le=600)

    @field_validator(
        "telegram_allowed_chat_id",
        "alexey_telegram_user_id",
        "andrey_telegram_user_id",
        mode="before",
    )
    @classmethod
    def empty_optional_id_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql+asyncpg://"):
            parts = urlsplit(value)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            sslmode = query.pop("sslmode", None)
            query.pop("channel_binding", None)  # libpq option unsupported by asyncpg
            if sslmode and sslmode != "disable":
                query["ssl"] = "require"
            value = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        return value

    @property
    def telegram_ready(self) -> bool:
        return bool(
            self.telegram_enabled
            and self.telegram_bot_token
            and self.telegram_allowed_chat_id is not None
        )

    @property
    def openai_ready(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
