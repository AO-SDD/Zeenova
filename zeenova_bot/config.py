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

    # Optional Telegram Premium custom-emoji IDs that replace the
    # in-body emojis on price cards, ``/market`` and ``/top``. Each one
    # is wrapped in a ``<tg-emoji emoji-id="...">…</tg-emoji>`` tag so
    # Premium clients show the configured custom emoji while older /
    # non-Premium clients fall back to the regular emoji. Same Premium
    # requirement as the brand-button icons: the bot owner needs a
    # Telegram Premium subscription. Leave any of these blank to keep
    # the corresponding emoji at its default look.
    premium_emoji_up_id: str = Field(default="")  # 🟢
    premium_emoji_down_id: str = Field(default="")  # 🔴
    # Optional override for the 24H Change line specifically. When set,
    # the price card uses this single Premium custom emoji on the
    # 24H Change row regardless of whether the 24h move is positive or
    # negative — the header dot keeps using the up/down pair above.
    # When blank, the 24H Change row falls back to the same up/down
    # logic as the header dot.
    premium_emoji_change_id: str = Field(default="")
    premium_emoji_rank_id: str = Field(default="")  # 🏆
    premium_emoji_price_id: str = Field(default="")  # 💵
    premium_emoji_high_id: str = Field(default="")  # 🔼
    premium_emoji_low_id: str = Field(default="")  # 🔽
    premium_emoji_mcap_id: str = Field(default="")  # 🏛
    premium_emoji_volume_id: str = Field(default="")  # 📊
    premium_emoji_globe_id: str = Field(default="")  # 🌐 — /market header
    premium_emoji_btc_id: str = Field(default="")  # 🟠 — BTC dominance
    premium_emoji_coins_id: str = Field(default="")  # 🪙 — active coins
    premium_emoji_fng_id: str = Field(default="")  # 😱/😨/😐/🙂/🤑 — Fear & Greed
    premium_emoji_top_id: str = Field(default="")  # 📈 — /top header
    premium_emoji_news_id: str = Field(default="")  # 📰 — /news header

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
