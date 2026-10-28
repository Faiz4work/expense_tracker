from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: str = Field(validation_alias="DATABASE_URL", repr=False)
    redis_url: str = Field(validation_alias="REDIS_URL", repr=False)
    redis_namespace: str = Field(
        default="expense-tracker",
        validation_alias="REDIS_NAMESPACE",
    )
    jwt_secret: SecretStr = Field(
        min_length=32,
        validation_alias="JWT_SECRET",
    )

    @field_validator("database_url")
    @classmethod
    def require_async_postgresql(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with postgresql+asyncpg://")
        return value

    @field_validator("redis_url")
    @classmethod
    def require_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
