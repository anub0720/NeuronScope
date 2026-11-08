"""Backend configuration."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):

    APP_NAME: str = "NeuronScope"

    DATABASE_URL: str

    SECRET_KEY: str
    DASHBOARD_BASE_URL: str = "http://localhost:5173"
    DASHBOARD_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    SESSION_COOKIE_SECURE: bool = True
    SESSION_TTL_SECONDS: int = Field(default=43200, ge=300, le=2592000)
    ACCOUNT_TOKEN_TTL_SECONDS: int = Field(default=3600, ge=300, le=86400)
    DEVICE_AUTH_TTL_SECONDS: int = Field(default=600, ge=60, le=1800)
    SMTP_HOST: str | None = None
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None
    SMTP_USE_TLS: bool = True
    METRIC_WINDOW_SIZE: int = Field(default=5, ge=1, le=100)
    MAX_ACTIVE_EXPERIMENTS: int = Field(default=100, ge=1)
    MAX_SUBSCRIBERS_PER_EXPERIMENT: int = Field(default=100, ge=1)
    MAX_NEURONS_PER_EXPERIMENT: int = Field(default=10000, ge=1)
    MAX_COLLAPSE_EVENTS_PER_EXPERIMENT: int = Field(default=2000, ge=1)
    MAX_EXPERIMENT_MEMORY_BYTES: int = Field(default=67108864, ge=1024)
    MAX_REQUEST_BYTES: int = Field(default=1048576, ge=1024)
    EXPERIMENT_IDLE_SECONDS: int = Field(default=1800, ge=1)
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()