"""In-memory mapping of ``(chat_id, user_msg_id) -> bot reply`` for the
edit-to-edit message flow.

Telegram delivers an ``edited_message`` update when a user edits one of
their previous messages. Without state we'd have to ignore the edit
(today's behaviour: send no response) or always send a *new* reply
(noisy, leaves the original reply stale). What we want instead is:

* find the previous reply we sent for the original message and
* either edit it in place, or delete+resend when the reply type
  changes (text → photo or vice versa).

This module records the link between the user's message id and the
reply id we produced. It's a bounded LRU so the bot doesn't grow
without limit in busy chats — edits attempted on messages that have
fallen out of the window simply fall back to sending a fresh reply.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final, Literal

__all__ = ["EditableReplyStore", "ReplyKind"]


ReplyKind = Literal["text", "photo"]

# Default LRU capacity. Each entry is ~96 bytes, so 5 000 entries
# comfortably fits in a few hundred KB. Bot owners with very busy
# group setups can override via :meth:`EditableReplyStore.__init__`.
_DEFAULT_CAPACITY: Final[int] = 5000


class EditableReplyStore:
    """Bounded LRU mapping a user message to the bot's reply.

    Keys are ``(chat_id, user_msg_id)`` tuples; values are
    ``(bot_msg_id, kind)`` where ``kind`` is ``"text"`` or ``"photo"``
    so the edit handler knows whether to call ``edit_message_text`` or
    ``edit_message_media``.

    Thread-safety: not thread-safe, but PTB serialises handler updates
    per chat (with ``concurrent_updates=True``, only unrelated chats
    run in parallel) so concurrent access for the same key is not
    expected in practice.
    """

    def __init__(self, max_entries: int = _DEFAULT_CAPACITY) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._cap = max_entries
        self._store: OrderedDict[
            tuple[int, int], tuple[int, ReplyKind]
        ] = OrderedDict()

    def __len__(self) -> int:  # noqa: D401 — len is conventional
        return len(self._store)

    def record(
        self,
        chat_id: int,
        user_msg_id: int,
        *,
        bot_msg_id: int,
        kind: ReplyKind,
    ) -> None:
        """Remember that ``bot_msg_id`` (of ``kind``) is the bot's reply
        to ``user_msg_id`` in ``chat_id``. Refreshes LRU position."""
        key = (chat_id, user_msg_id)
        self._store[key] = (bot_msg_id, kind)
        self._store.move_to_end(key)
        while len(self._store) > self._cap:
            self._store.popitem(last=False)

    def get(
        self, chat_id: int, user_msg_id: int
    ) -> tuple[int, ReplyKind] | None:
        """Return the existing reply for ``user_msg_id`` or ``None``.

        Touches LRU order on hit so frequently-edited messages keep
        their reply mapping alive even in busy chats.
        """
        key = (chat_id, user_msg_id)
        value = self._store.get(key)
        if value is not None:
            self._store.move_to_end(key)
        return value

    def pop(
        self, chat_id: int, user_msg_id: int
    ) -> tuple[int, ReplyKind] | None:
        """Forget the existing reply, returning it if there was one."""
        return self._store.pop((chat_id, user_msg_id), None)

    def clear(self) -> None:
        self._store.clear()
