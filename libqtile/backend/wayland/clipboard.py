"""
The clipboard for the Wayland backend's portal.

The xdg-desktop-portal Clipboard backend lets an input capture session own the seat's
clipboard selection, with data it supplies only when a client pastes, and read whatever
the current selection offers. The compositor side lives in qw/clipboard.c; this module
wraps it for the portal.

Callbacks from the compositor run inside its calls, so they only record state; selection
listeners are called from the event loop after the change instead.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

try:
    from libqtile.backend.wayland._ffi import ffi, lib
except ModuleNotFoundError:
    from libqtile.backend.wayland.ffi_stub import ffi, lib

if TYPE_CHECKING:
    from libqtile.backend.wayland.core import Core

MIME_TYPE_BUFFER = 16


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
        if not mime_types:
            raise ValueError("A clipboard source needs at least one mime type")
        self.core = core
        self.on_send = on_send
        self.on_cancelled = on_cancelled

        # Kept alive by core.clipboard_sources until the C side cancels the source
        self._handle = ffi.new_handle(self)
        encoded = [ffi.new("char[]", mime_type.encode()) for mime_type in mime_types]
        self._ptr = lib.qw_clipboard_set_selection(
            core.qw, ffi.new("const char *[]", encoded), len(encoded), self._handle
        )
        if self._ptr == ffi.NULL:
            raise RuntimeError("Could not create clipboard source")
        core.clipboard_sources.add(self)

    @property
    def alive(self) -> bool:
        """Whether this is still the selection."""
        return self._ptr != ffi.NULL

    def destroy(self) -> None:
        """Stop being the selection, if it still is; on_cancelled is called first."""
        if self.alive:
            lib.qw_clipboard_source_destroy(self._ptr)

    def _cancelled(self) -> None:
        self._ptr = ffi.NULL
        self.core.clipboard_sources.discard(self)
        if self.on_cancelled is not None:
            self.on_cancelled()


def read_selection(core: Core, mime_type: str) -> int | None:
    """
    The read end of a pipe the current selection writes its data for the mime type into,
    which the caller owns, or None if there is no selection or it does not offer it.
    """
    read_fd, write_fd = os.pipe()
    if lib.qw_clipboard_send_selection(core.qw, mime_type.encode(), write_fd):
        return read_fd
    os.close(read_fd)
    return None


def selection_mime_types(core: Core) -> list[str]:
    """The current selection's mime types, empty if there is no selection."""
    buffer = ffi.new("const char *[]", MIME_TYPE_BUFFER)
    count = lib.qw_clipboard_get_mime_types(core.qw, buffer, MIME_TYPE_BUFFER)
    if count > MIME_TYPE_BUFFER:
        buffer = ffi.new("const char *[]", count)
        count = lib.qw_clipboard_get_mime_types(core.qw, buffer, count)
    return [ffi.string(buffer[i]).decode() for i in range(count)]


def selection_owner(core: Core) -> ClipboardSource | None:
    """The source that is the current selection, if one is."""
    userdata = lib.qw_clipboard_get_selection_userdata(core.qw)
    if userdata == ffi.NULL:
        return None
    return ffi.from_handle(userdata)


def add_selection_listener(core: Core, listener: Callable[[], None]) -> None:
    """Call listener() after every change of the selection, including a clear."""
    core.clipboard_listeners.add(listener)


def remove_selection_listener(core: Core, listener: Callable[[], None]) -> None:
    """Stop calling a listener added with add_selection_listener."""
    core.clipboard_listeners.discard(listener)


def _notify_listeners(core: Core) -> None:
    for listener in list(core.clipboard_listeners):
        listener()


@ffi.def_extern()
def clipboard_source_send_cb(userdata: ffi.CData, mime_type: ffi.CData, fd: int) -> None:
    source = ffi.from_handle(userdata)
    source.on_send(ffi.string(mime_type).decode(), fd)


@ffi.def_extern()
def clipboard_source_cancelled_cb(userdata: ffi.CData) -> None:
    ffi.from_handle(userdata)._cancelled()


@ffi.def_extern()
def clipboard_selection_changed_cb(userdata: ffi.CData) -> None:
    core = ffi.from_handle(userdata)
    if core.clipboard_listeners:
        asyncio.get_running_loop().call_soon(_notify_listeners, core)
