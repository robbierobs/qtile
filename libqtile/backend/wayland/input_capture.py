"""
Input capture sessions for the Wayland backend.

An input capture session hands pointer and keyboard input to another program, such as
Synergy or Deskflow sharing this machine's keyboard and mouse, over an EIS (libei)
connection. The compositor side lives in qw/input-capture.c; this module wraps it for the
xdg-desktop-portal InputCapture backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from libqtile.log_utils import logger

try:
    from libqtile.backend.wayland._ffi import ffi, lib
except ModuleNotFoundError:
    from libqtile.backend.wayland.ffi_stub import ffi, lib

if TYPE_CHECKING:
    from libqtile.backend.wayland.core import Core

MAX_ZONES = 16


@dataclass(frozen=True)
class Zone:
    """An output in layout coordinates, as the portal describes it."""

    width: int
    height: int
    x: int
    y: int


@dataclass(frozen=True)
class Barrier:
    """A horizontal or vertical line on the outer edge of the zones."""

    barrier_id: int
    x1: int
    y1: int
    x2: int
    y2: int


def get_zones(core: Core) -> list[Zone]:
    boxes = ffi.new("struct wlr_box[]", MAX_ZONES)
    count = lib.qw_input_capture_get_zones(core.qw, boxes, MAX_ZONES)
    return [Zone(b.width, b.height, b.x, b.y) for b in boxes[0:count]]


def _covered(zones: list[Zone], x: int, y: int) -> bool:
    return any(z.x <= x < z.x + z.width and z.y <= y < z.y + z.height for z in zones)


def barrier_is_valid(barrier: Barrier, zones: list[Zone]) -> bool:
    """
    Whether the barrier lies along one zone's edge and on the outer boundary of all zones,
    as the InputCapture portal requires. Barriers sit on the top or left edge of their
    pixels, so a zone's right edge is at x + width and its bottom edge at y + height.
    """
    x1, x2 = sorted((barrier.x1, barrier.x2))
    y1, y2 = sorted((barrier.y1, barrier.y2))

    if x1 == x2:
        for zone in zones:
            if not (zone.y <= y1 and y2 < zone.y + zone.height):
                continue
            if x1 == zone.x:
                outside = x1 - 1
            elif x1 == zone.x + zone.width:
                outside = x1
            else:
                continue
            # The pixels just across the edge must not belong to another zone
            if not any(_covered(zones, outside, y) for y in range(y1, y2 + 1)):
                return True
        return False

    if y1 == y2:
        for zone in zones:
            if not (zone.x <= x1 and x2 < zone.x + zone.width):
                continue
            if y1 == zone.y:
                outside = y1 - 1
            elif y1 == zone.y + zone.height:
                outside = y1
            else:
                continue
            if not any(_covered(zones, x, outside) for x in range(x1, x2 + 1)):
                return True
        return False

    return False


class InputCaptureSession:
    """One portal input capture session."""

    def __init__(
        self,
        core: Core,
        on_activated: Callable[[int, int, float, float], None] | None = None,
        on_deactivated: Callable[[int], None] | None = None,
        on_disabled: Callable[[], None] | None = None,
        on_zones_changed: Callable[[], None] | None = None,
    ) -> None:
        self.core = core
        self.on_activated = on_activated
        self.on_deactivated = on_deactivated
        self.on_disabled = on_disabled
        self.on_zones_changed = on_zones_changed

        # Kept alive for as long as the C side may call back with it
        self._handle = ffi.new_handle(self)
        self._ptr = lib.qw_input_capture_create(core.qw, self._handle)
        if self._ptr == ffi.NULL:
            raise RuntimeError("Could not create input capture session")
        core.input_capture_sessions.add(self)

    def connect_eis(self) -> int:
        """Return the client end of the EIS connection, which the caller then owns."""
        fd = lib.qw_input_capture_connect_eis(self._ptr)
        if fd < 0:
            raise RuntimeError("Could not connect input capture session to EIS")
        return fd

    def set_barriers(self, barriers: list[Barrier]) -> list[int]:
        """Replace the barriers, returning the IDs of those that are invalid."""
        zones = get_zones(self.core)
        failed = []
        lib.qw_input_capture_clear_barriers(self._ptr)
        for barrier in barriers:
            if not barrier_is_valid(barrier, zones) or not lib.qw_input_capture_add_barrier(
                self._ptr, barrier.barrier_id, barrier.x1, barrier.y1, barrier.x2, barrier.y2
            ):
                logger.debug("Rejected input capture barrier: %s", barrier)
                failed.append(barrier.barrier_id)
        return failed

    def enable(self) -> None:
        lib.qw_input_capture_enable(self._ptr)

    def disable(self) -> None:
        lib.qw_input_capture_disable(self._ptr)

    def release(self, activation_id: int, position: tuple[float, float] | None = None) -> None:
        x, y = position if position is not None else (0.0, 0.0)
        lib.qw_input_capture_release(self._ptr, activation_id, position is not None, x, y)

    def close(self) -> None:
        if self._ptr == ffi.NULL:
            return
        lib.qw_input_capture_destroy(self._ptr)
        self._ptr = ffi.NULL
        self.core.input_capture_sessions.discard(self)


@ffi.def_extern()
def input_capture_activated_cb(
    userdata: ffi.CData, activation_id: int, barrier_id: int, x: float, y: float
) -> None:
    session = ffi.from_handle(userdata)
    if session.on_activated is not None:
        session.on_activated(activation_id, barrier_id, x, y)


@ffi.def_extern()
def input_capture_deactivated_cb(userdata: ffi.CData, activation_id: int) -> None:
    session = ffi.from_handle(userdata)
    if session.on_deactivated is not None:
        session.on_deactivated(activation_id)


@ffi.def_extern()
def input_capture_disabled_cb(userdata: ffi.CData) -> None:
    session = ffi.from_handle(userdata)
    if session.on_disabled is not None:
        session.on_disabled()


@ffi.def_extern()
def input_capture_zones_changed_cb(userdata: ffi.CData) -> None:
    session = ffi.from_handle(userdata)
    if session.on_zones_changed is not None:
        session.on_zones_changed()
