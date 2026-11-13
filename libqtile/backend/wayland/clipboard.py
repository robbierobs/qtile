"""
The clipboard for the Wayland backend's portal.

The xdg-desktop-portal Clipboard backend lets an input capture session own the seat's
clipboard selection, with data it supplies only when a client pastes, and read whatever
the current selection offers. The compositor side lives in qw/clipboard.c; this module
wraps it for the portal.

This module holds the interface the portal is written against. Its bodies are
placeholders until the compositor side lands: no source can be created, there is never a
selection to read, and listeners are never called.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libqtile.backend.wayland.core import Core


class ClipboardSource:
    """The seat's clipboard selection, owned on behalf of a portal session."""

    def __init__(
        self,
        core: Core,
        mime_types: list[str],
        on_send: Callable[[str, int], None],
        on_cancelled: Callable[[], None] | None = None,
    ) -> None:
        """
        Become the selection, offering the mime types, which must not be empty.

        on_send(mime_type, fd) is called when a client pastes: the callee owns fd, writes
        the data for that mime type into it and closes it, now or later. on_cancelled() is
        called exactly once when this stops being the selection, whether replaced by
        another client, cleared, or destroyed; the source is dead afterwards.
        """
        raise RuntimeError("Clipboard sources are not implemented yet")

    @property
    def alive(self) -> bool:
        """Whether this is still the selection."""
        return False

    def destroy(self) -> None:
        """Stop being the selection, if it still is; on_cancelled is called first."""


def read_selection(core: Core, mime_type: str) -> int | None:
    """
    The read end of a pipe the current selection writes its data for the mime type into,
    which the caller owns, or None if there is no selection or it does not offer it.
    """
    return None


def selection_mime_types(core: Core) -> list[str]:
    """The current selection's mime types, empty if there is no selection."""
    return []


def selection_owner(core: Core) -> ClipboardSource | None:
    """The source that is the current selection, if one is."""
    return None


def add_selection_listener(core: Core, listener: Callable[[], None]) -> None:
    """Call listener() after every change of the selection, including a clear."""


def remove_selection_listener(core: Core, listener: Callable[[], None]) -> None:
    """Stop calling a listener added with add_selection_listener."""
