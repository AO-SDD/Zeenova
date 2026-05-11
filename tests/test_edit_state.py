"""Tests for the bounded LRU reply store used by edit-to-edit support."""

from __future__ import annotations

import pytest

from zeenova_bot.edit_state import EditableReplyStore


class TestEditableReplyStore:
    def test_record_and_get_roundtrip(self) -> None:
        store = EditableReplyStore()
        store.record(1, 100, bot_msg_id=999, kind="text")
        assert store.get(1, 100) == (999, "text")

    def test_get_missing_returns_none(self) -> None:
        store = EditableReplyStore()
        assert store.get(1, 100) is None

    def test_record_overwrites_previous_entry(self) -> None:
        store = EditableReplyStore()
        store.record(1, 100, bot_msg_id=999, kind="text")
        store.record(1, 100, bot_msg_id=1234, kind="photo")
        assert store.get(1, 100) == (1234, "photo")

    def test_pop_returns_and_removes(self) -> None:
        store = EditableReplyStore()
        store.record(1, 100, bot_msg_id=999, kind="text")
        popped = store.pop(1, 100)
        assert popped == (999, "text")
        assert store.get(1, 100) is None

    def test_pop_missing_returns_none(self) -> None:
        store = EditableReplyStore()
        assert store.pop(1, 100) is None

    def test_chat_id_isolation(self) -> None:
        store = EditableReplyStore()
        store.record(1, 100, bot_msg_id=999, kind="text")
        store.record(2, 100, bot_msg_id=888, kind="photo")
        assert store.get(1, 100) == (999, "text")
        assert store.get(2, 100) == (888, "photo")

    def test_lru_eviction(self) -> None:
        store = EditableReplyStore(max_entries=3)
        store.record(1, 1, bot_msg_id=11, kind="text")
        store.record(1, 2, bot_msg_id=22, kind="text")
        store.record(1, 3, bot_msg_id=33, kind="text")
        # Touch (1, 1) so it becomes most-recent.
        assert store.get(1, 1) == (11, "text")
        # Add a fourth — oldest is (1, 2), should be evicted.
        store.record(1, 4, bot_msg_id=44, kind="text")
        assert store.get(1, 1) == (11, "text")
        assert store.get(1, 2) is None
        assert store.get(1, 3) == (33, "text")
        assert store.get(1, 4) == (44, "text")
        assert len(store) == 3

    def test_rejects_zero_capacity(self) -> None:
        with pytest.raises(ValueError):
            EditableReplyStore(max_entries=0)

    def test_clear(self) -> None:
        store = EditableReplyStore()
        store.record(1, 100, bot_msg_id=999, kind="text")
        store.record(1, 101, bot_msg_id=1000, kind="photo")
        store.clear()
        assert len(store) == 0
        assert store.get(1, 100) is None
