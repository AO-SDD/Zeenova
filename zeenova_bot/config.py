"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(..., description="Token from @BotFather")
    coingecko_api_key: str = Field(default="", description="Optional CoinGecko Pro key")
    allowed_chat_ids: str = Field(
        default="",
        description="Optional comma-separated chat IDs the bot may respond to",
    )

    brand_name: str = Field(default="Zeenova")
    telegram_channel_url: str = Field(default="https://t.me/ox_zeen")
    telegram_group_url: str = Field(default="https://t.me/blockzeen")

    log_level: str = Field(default="INFO")

    @property
    def allowed_chat_id_set(self) -> set[int]:
        if not self.allowed_chat_ids.strip():
            return set()
        out: set[int] = set()
        for raw in self.allowed_chat_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.add(int(raw))
            except ValueError:
                continue
        return out


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
