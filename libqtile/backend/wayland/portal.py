"""
An xdg-desktop-portal backend for the Wayland backend.

It implements org.freedesktop.impl.portal.InputCapture, which programs such as Synergy
and Deskflow use to share this machine's keyboard and mouse with others. xdg-desktop-portal
routes the interface here when qtile.portal is installed and the session's portals.conf
selects it:

    [preferred]
    org.freedesktop.impl.portal.InputCapture=qtile

Only the programs listed in ``wl_input_capture_apps`` may capture input, matched by app
ID or by executable. Programs started outside a sandbox usually have no app ID, so the
executable of the D-Bus connection that asked is checked as well.
"""

import os
from typing import TYPE_CHECKING, Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag, PropertyAccess, RequestNameReply
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from libqtile.backend.wayland.input_capture import (
    Barrier,
    InputCaptureSession,
    Zone,
    get_zones,
)
from libqtile.log_utils import logger

if TYPE_CHECKING:
    from libqtile.backend.wayland.core import Core

BUS_NAME = "org.freedesktop.impl.portal.desktop.qtile"
OBJECT_PATH = "/org/freedesktop/portal/desktop"

# Portal response codes
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1
RESPONSE_OTHER = 2

# InputCapture capabilities; touchscreens are not captured
CAPABILITY_KEYBOARD = 1
CAPABILITY_POINTER = 2
SUPPORTED_CAPABILITIES = CAPABILITY_KEYBOARD | CAPABILITY_POINTER


def sender_from_session_handle(session_handle: str) -> str | None:
    """
    The unique bus name of the program that owns a session, which xdg-desktop-portal
    puts in the handle: /org/freedesktop/portal/desktop/session/1_42/token is :1.42's.
    """
    parts = session_handle.split("/")
    if len(parts) < 2 or parts[-3] != "session":
        return None
    return ":" + parts[-2].replace("_", ".")


class PortalSession(ServiceInterface):
    """The org.freedesktop.impl.portal.Session object of one input capture session."""

    def __init__(self, portal: "InputCapturePortal", handle: str, app_id: str) -> None:
        super().__init__("org.freedesktop.impl.portal.Session")
        self.portal = portal
        self.handle = handle
        self.app_id = app_id
        self.started = False
        # The zones this session last knew about, reported as invalid when they change
        self.zone_set = portal.zone_set
        self.capture: InputCaptureSession | None = None
        self.eis_fd: int | None = None

    def start(self) -> None:
        interface = self.portal.interface
        self.capture = InputCaptureSession(
            self.portal.core,
            on_activated=lambda activation_id, barrier_id, x, y: interface.Activated(
                self.handle,
                {
                    "activation_id": Variant("u", activation_id),
                    "barrier_id": Variant("u", barrier_id),
                    "cursor_position": Variant("(dd)", [x, y]),
                },
            ),
            on_deactivated=lambda activation_id: interface.Deactivated(
                self.handle, {"activation_id": Variant("u", activation_id)}
            ),
            on_disabled=lambda: interface.Disabled(self.handle, {}),
            on_zones_changed=lambda: self.portal.zones_changed(self),
        )
        self.started = True

    def close(self) -> None:
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        # Kept open until now; dbus_fast does not close fds it sends
        if self.eis_fd is not None:
            os.close(self.eis_fd)
            self.eis_fd = None
        self.portal.remove_session(self)

    @method()
    def Close(self) -> None:  # noqa: N802
        logger.debug("Input capture session closed: %s", self.handle)
        self.close()

    @signal()
    def Closed(self) -> None:  # noqa: N802
        pass

    @dbus_property(access=PropertyAccess.READ)
    def version(self) -> "u":  # type:ignore  # noqa: F722, F821
        return 1


class InputCaptureInterface(ServiceInterface):
    """org.freedesktop.impl.portal.InputCapture, version 2."""

    def __init__(self, portal: "InputCapturePortal") -> None:
        super().__init__("org.freedesktop.impl.portal.InputCapture")
        self.portal = portal

    @dbus_property(access=PropertyAccess.READ)
    def SupportedCapabilities(self) -> "u":  # type:ignore  # noqa: F722, F821, N802
        return SUPPORTED_CAPABILITIES

    @dbus_property(access=PropertyAccess.READ)
    def version(self) -> "u":  # type:ignore  # noqa: F722, F821
        return 2

    @method()
    async def CreateSession(  # noqa: N802
        self,
        handle: "o",  # type:ignore  # noqa: F722, F821
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        parent_window: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        # Version 1 creates and starts the session in one go
        capabilities = options.get("capabilities", Variant("u", 0)).value
        session = await self.portal.create_session(session_handle, app_id)
        if session is None:
            return [RESPONSE_OTHER, {}]
        if not await self.portal.start_session(session, capabilities):
            session.close()
            return [RESPONSE_OTHER, {}]
        return [
            RESPONSE_SUCCESS,
            {
                "session_id": Variant("s", session_handle),
                "capabilities": Variant("u", capabilities & SUPPORTED_CAPABILITIES),
            },
        ]

    @method()
    async def CreateSession2(  # noqa: N802
        self,
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "a{sv}":  # type:ignore  # noqa: F722, F821
        await self.portal.create_session(session_handle, app_id)
        return {}

    @method()
    async def Start(  # noqa: N802
        self,
        handle: "o",  # type:ignore  # noqa: F722, F821
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        parent_window: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        session = self.portal.sessions.get(session_handle)
        capabilities = options.get("capabilities", Variant("u", 0)).value
        if session is None or session.started:
            return [RESPONSE_OTHER, {}]
        if not await self.portal.start_session(session, capabilities):
            return [RESPONSE_OTHER, {}]
        # Neither the clipboard nor persistence are offered
        return [
            RESPONSE_SUCCESS,
            {"capabilities": Variant("u", capabilities & SUPPORTED_CAPABILITIES)},
        ]

    @method()
    def GetZones(  # noqa: N802
        self,
        handle: "o",  # type:ignore  # noqa: F722, F821
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        if self.portal.capture_for(session_handle) is None:
            return [RESPONSE_OTHER, {}]
        zones = self.portal.zones()
        self.portal.sessions[session_handle].zone_set = self.portal.zone_set
        return [
            RESPONSE_SUCCESS,
            {
                "zones": Variant("a(uuii)", [[z.width, z.height, z.x, z.y] for z in zones]),
                "zone_set": Variant("u", self.portal.zone_set),
            },
        ]

    @method()
    def SetPointerBarriers(  # noqa: N802
        self,
        handle: "o",  # type:ignore  # noqa: F722, F821
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
        barriers: "aa{sv}",  # type:ignore  # noqa: F722, F821
        zone_set: "u",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        capture = self.portal.capture_for(session_handle)
        if capture is None:
            return [RESPONSE_OTHER, {}]

        requested = []
        failed = []
        for barrier in barriers:
            barrier_id = barrier["barrier_id"].value
            position = barrier["position"].value
            requested.append(Barrier(barrier_id, *position))

        if zone_set != self.portal.zone_set:
            # Barriers for zones that no longer exist all fail
            capture.set_barriers([])
            failed = [barrier.barrier_id for barrier in requested]
        else:
            failed = capture.set_barriers(requested)

        return [RESPONSE_SUCCESS, {"failed_barriers": Variant("au", failed)}]

    @method()
    def Enable(  # noqa: N802
        self,
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        capture = self.portal.capture_for(session_handle)
        if capture is None:
            return [RESPONSE_OTHER, {}]
        capture.enable()
        return [RESPONSE_SUCCESS, {}]

    @method()
    def Disable(  # noqa: N802
        self,
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        capture = self.portal.capture_for(session_handle)
        if capture is None:
            return [RESPONSE_OTHER, {}]
        capture.disable()
        return [RESPONSE_SUCCESS, {}]

    @method()
    def Release(  # noqa: N802
        self,
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "ua{sv}":  # type:ignore  # noqa: F722, F821
        capture = self.portal.capture_for(session_handle)
        if capture is None or "activation_id" not in options:
            return [RESPONSE_OTHER, {}]
        position = options.get("cursor_position")
        capture.release(
            options["activation_id"].value,
            tuple(position.value) if position is not None else None,
        )
        return [RESPONSE_SUCCESS, {}]

    @method()
    def ConnectToEIS(  # noqa: N802
        self,
        session_handle: "o",  # type:ignore  # noqa: F722, F821
        app_id: "s",  # type:ignore  # noqa: F722, F821
        options: "a{sv}",  # type:ignore  # noqa: F722, F821
    ) -> "h":  # type:ignore  # noqa: F722, F821
        session = self.portal.sessions.get(session_handle)
        if session is None or session.capture is None or session.eis_fd is not None:
            raise ValueError("No started input capture session to connect")
        session.eis_fd = session.capture.connect_eis()
        return session.eis_fd

    @signal()
    def Disabled(self, session_handle: str, options: dict) -> "oa{sv}":  # type:ignore  # noqa: F722, F821, N802
        return [session_handle, options]

    @signal()
    def Activated(self, session_handle: str, options: dict) -> "oa{sv}":  # type:ignore  # noqa: F722, F821, N802
        return [session_handle, options]

    @signal()
    def Deactivated(self, session_handle: str, options: dict) -> "oa{sv}":  # type:ignore  # noqa: F722, F821, N802
        return [session_handle, options]

    @signal()
    def ZonesChanged(self, session_handle: str, options: dict) -> "oa{sv}":  # type:ignore  # noqa: F722, F821, N802
        return [session_handle, options]


class InputCapturePortal:
    """Owns the portal's bus name and its sessions."""

    def __init__(self, core: "Core", allowed_apps: list[str]) -> None:
        self.core = core
        self.allowed_apps = allowed_apps
        self.interface = InputCaptureInterface(self)
        self.sessions: dict[str, PortalSession] = {}
        self.bus: MessageBus | None = None
        # Increases whenever the zones change, so stale barriers can be refused
        self.zone_set = 1
        self._zones = self.zones()

    async def start(self) -> bool:
        try:
            self.bus = await MessageBus(negotiate_unix_fd=True).connect()
        except Exception:
            logger.exception("Could not connect to the session bus for the portal")
            return False

        self.bus.export(OBJECT_PATH, self.interface)
        reply = await self.bus.request_name(BUS_NAME, flags=NameFlag.DO_NOT_QUEUE)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            logger.warning("Cannot start the portal, %s is already owned", BUS_NAME)
            self.bus.disconnect()
            self.bus = None
            return False

        logger.info("Input capture portal started for: %s", ", ".join(self.allowed_apps))
        return True

    def stop(self) -> None:
        for session in list(self.sessions.values()):
            session.close()
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    def zones(self) -> list[Zone]:
        return get_zones(self.core)

    def zones_changed(self, session: "PortalSession") -> None:
        # Every session reports the change, but the zones are shared
        zones = self.zones()
        if zones != self._zones:
            self._zones = zones
            self.zone_set += 1
        invalidated = session.zone_set
        session.zone_set = self.zone_set
        self.interface.ZonesChanged(session.handle, {"zone_set": Variant("u", invalidated)})

    def capture_for(self, session_handle: str) -> InputCaptureSession | None:
        session = self.sessions.get(session_handle)
        return session.capture if session is not None else None

    async def create_session(self, session_handle: str, app_id: str) -> PortalSession | None:
        assert self.bus is not None
        if session_handle in self.sessions:
            return None
        session = PortalSession(self, session_handle, app_id)
        self.bus.export(session_handle, session)
        self.sessions[session_handle] = session
        return session

    def remove_session(self, session: PortalSession) -> None:
        if self.sessions.pop(session.handle, None) is not None and self.bus is not None:
            self.bus.unexport(session.handle, session)

    async def start_session(self, session: PortalSession, capabilities: int) -> bool:
        if not capabilities & SUPPORTED_CAPABILITIES:
            return False
        if not await self.is_allowed(session):
            return False
        session.start()
        logger.info("Input capture session started for %s", session.app_id or session.handle)
        return True

    async def _executable(self, session_handle: str) -> str | None:
        """The executable behind the D-Bus connection that owns the session."""
        sender = sender_from_session_handle(session_handle)
        if sender is None or self.bus is None:
            return None
        reply = await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="GetConnectionUnixProcessID",
                signature="s",
                body=[sender],
            )
        )
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return None
        try:
            return os.readlink(f"/proc/{reply.body[0]}/exe")
        except OSError:
            return None

    async def is_allowed(self, session: PortalSession) -> bool:
        executable = await self._executable(session.handle)
        candidates: list[Any] = [session.app_id, executable]
        if executable is not None:
            candidates.append(os.path.basename(executable))
        allowed = any(c and c in self.allowed_apps for c in candidates)
        log = logger.info if allowed else logger.warning
        log(
            "Input capture %s for app ID %r, executable %r",
            "allowed" if allowed else "refused",
            session.app_id,
            executable,
        )
        return allowed
