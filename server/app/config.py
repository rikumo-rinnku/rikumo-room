from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret: str = "development-only-change-me"
    database_path: str = "./data/rikumo-room.sqlite3"
    app_timezone: str = "Asia/Shanghai"
    superadmin_username: str = Field(default="璃雲", min_length=1, max_length=48)
    superadmin_password: str = Field(default="change-me", min_length=4, max_length=200)
    livekit_api_url: str = "http://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "secret"
    livekit_public_url: str = "ws://localhost:7880"
    session_cookie_secure: bool = False
    session_cookie_name: str = "rikumo_session"
    guest_session_hours: int = 16
    admin_session_days: int = 14

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
