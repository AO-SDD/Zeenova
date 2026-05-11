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
    # Names rendered in the price card footer. Independent of ``brand_name``
    # so the chart watermark can stay "ZEENOVA" while the footer reads
    # "Zeen Channel" / "Zeen Chat".
    channel_name: str = Field(default="Zeen Channel")
    group_name: str = Field(default="Zeen Chat")
    telegram_channel_url: str = Field(default="https://t.me/ox_zeen")
    telegram_group_url: str = Field(default="https://t.me/blockzeen")
    # Optional Telegram Premium custom-emoji IDs that decorate the
    # ``📣 Zeen Channel`` and ``💬 Zeen Chat`` shortcut buttons. Telegram
    # accepts ``icon_custom_emoji_id`` only when the bot owner has a
    # Premium subscription (or the bot bought a Fragment username). Leave
    # blank to render the buttons without a custom icon — the regular
    # emoji in the label is shown either way.
    brand_channel_emoji_id: str = Field(default="")
    brand_group_emoji_id: str = Field(default="")

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
