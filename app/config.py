from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_allowed_chat_id: int | None = None
    alexey_telegram_user_id: int | None = None
    andrey_telegram_user_id: int | None = None
    telegram_enabled: bool = True

    openai_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./sam.sqlite3"

    sam_model: str = "gpt-5.4-mini"
    extraction_model: str = "gpt-5.4-mini"
    transcription_model: str = "gpt-4o-mini-transcribe"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embeddings_enabled: bool = True

    timezone: str = "America/Toronto"
    debug: bool = False
    recent_message_limit: int = Field(default=24, ge=5, le=100)

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
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
