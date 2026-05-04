"""Tests for environment-driven settings."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from zeenova_bot.config import Settings


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "COINGECKO_API_KEY",
        "ALLOWED_CHAT_IDS",
        "BRAND_NAME",
        "TELEGRAM_CHANNEL_URL",
        "TELEGRAM_GROUP_URL",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_settings_parses_allowed_chat_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "123, -456 , garbage, 789")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allowed_chat_id_set == {123, -456, 789}


def test_settings_empty_allowed_chat_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allowed_chat_id_set == set()


def test_settings_requires_token() -> None:
    # Make sure no .env in cwd leaks the token in
    cwd_env = os.path.join(os.getcwd(), ".env")
    cleanup = False
    if os.path.exists(cwd_env):
        os.rename(cwd_env, cwd_env + ".bak")
        cleanup = True
    try:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]
    finally:
        if cleanup:
            os.rename(cwd_env + ".bak", cwd_env)


def test_settings_branding_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.brand_name == "Zeenova"
    assert s.telegram_channel_url == "https://t.me/ox_zeen"
    assert s.telegram_group_url == "https://t.me/blockzeen"
