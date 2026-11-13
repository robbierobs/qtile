"""
In-process helpers for test_clipboard.py.

The tests call these through wmanager.c.eval, so they run inside the nested Qtile. They
record what happened and schedule work on the loop; they never block on a Wayland client.
State lives in this module, which persists in that process for the length of one test.
"""

import asyncio
import os

from libqtile.backend.wayland.clipboard import (
    ClipboardSource,
    add_selection_listener,
    read_selection,
    selection_owner,
)

events: list[tuple] = []
sources: dict[str, ClipboardSource] = {}
reads: dict[str, str | None] = {}
listener_calls: list[tuple] = []
_constructing = [False]


def offer(core, name, mime_types, payload):
    """Make a source offering mime_types whose data is payload, written on each paste."""

    def on_send(mime_type, fd):
        events.append(("send", name, mime_type))
        asyncio.get_running_loop().call_soon(_write, fd, payload.encode())

    def on_cancelled():
        events.append(("cancelled", name))

    _constructing[0] = True
    try:
        sources[name] = ClipboardSource(core, mime_types, on_send, on_cancelled)
    finally:
        _constructing[0] = False


def _write(fd, data):
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def destroy(name):
    sources[name].destroy()


def start_read(core, name, mime_type):
    """Read the selection's mime_type in the background; reads[name] is set when done."""
    fd = read_selection(core, mime_type)
    if fd is None:
        reads[name] = None
        return

    loop = asyncio.get_running_loop()
    chunks = []

    def on_readable():
        chunk = os.read(fd, 4096)
        if chunk:
            chunks.append(chunk)
            return
        loop.remove_reader(fd)
        os.close(fd)
        reads[name] = b"".join(chunks).decode()

    loop.add_reader(fd, on_readable)


def owner_name(core):
    owner = selection_owner(core)
    return next((name for name, source in sources.items() if source is owner), None)


def add_recording_listener(core, name):
    """Record each selection change: whether a source was being constructed, and the owner."""

    def listener():
        listener_calls.append((name, _constructing[0], owner_name(core)))

    add_selection_listener(core, listener)


def fd_count():
    return len(os.listdir("/proc/self/fd"))
