import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from dbus_fast import DBusError, ErrorType, Message, Variant
from dbus_fast.aio import MessageBus

from libqtile.config import Key
from libqtile.lazy import lazy
from test.backend.wayland.conftest import (
    CLIPBOARD_HELPER,
    ClientHandler,
    list_types,
    make_test_env,
    settled_fd_count,
)
from test.backend.wayland.test_input_capture import EIReceiver, zone
from test.helpers import BareConfig, Retry

BUS_NAME = "org.freedesktop.impl.portal.desktop.qtile"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_FILE = Path(__file__).parents[3] / "libqtile" / "resources" / "portal" / "qtile.portal"

# The tests talk to the portal from this process, which the allowlist sees by executable
TEST_EXECUTABLE = os.readlink("/proc/self/exe")
CAPABILITIES = 3  # keyboard and pointer
MIME_TYPES = ["text/plain;charset=utf-8", "text/plain"]


class PortalConfig(BareConfig):
    keys = [Key(["control"], key, lazy.group[key].toscreen()) for key in "abcd"]
    wl_input_capture_apps = [TEST_EXECUTABLE]


class RefusingPortalConfig(PortalConfig):
    wl_input_capture_apps = ["com.example.Allowed"]


# No <servicedir>, so nothing can be D-Bus activated. With the standard session
# configuration, xdg-desktop-portal activates the real xdg-document-portal, which mounts
# over the user's own $XDG_RUNTIME_DIR/doc and unmounts it on exit, breaking every
# Flatpak app in the running session.
PRIVATE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={tmp}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def private_bus(monkeypatch, tmp_path):
    """A session bus of its own, so tests never touch the real portal or its services."""
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is not installed")
    config = tmp_path / "bus.conf"
    config.write_text(PRIVATE_BUS_CONFIG.format(tmp=tmp_path))
    daemon = subprocess.Popen(
        [
            "dbus-daemon",
            f"--config-file={config}",
            "--nofork",
            "--nopidfile",
            "--print-address=1",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    address = daemon.stdout.readline().strip()
    # Qtile is started after this and inherits it
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    daemon.terminate()
    daemon.wait()
    daemon.stdout.close()


@pytest.fixture
def run():
    """
    Runs coroutines on one event loop for the whole test. Qtile's command client runs
    its own loop, so it is used between these calls rather than inside a coroutine.
    """
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


class Bus:
    """A connection to the private bus for the test to call portals with."""

    def __init__(self, run, address):
        self.run = run

        async def connect():
            # MessageBus needs a running loop to be created
            return await MessageBus(bus_address=address, negotiate_unix_fd=True).connect()

        self.bus = run(connect())

    @property
    def sender(self):
        # As xdg-desktop-portal puts it in object paths
        return self.bus.unique_name[1:].replace(".", "_")

    def interface(self, name, path, interface):
        introspection = self.run(self.bus.introspect(name, path))
        return self.bus.get_proxy_object(name, path, introspection).get_interface(interface)

    def call(self, member, *body, signature):
        reply = self.run(
            self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member=member,
                    signature=signature,
                    body=list(body),
                )
            )
        )
        return reply.body

    def wait_until(self, condition, timeout=5):
        """Let signals arrive until the condition holds."""
        for _ in range(int(timeout * 10)):
            if condition():
                return
            self.run(asyncio.sleep(0.1))
        raise AssertionError("condition not met")

    def close(self):
        self.bus.disconnect()


@pytest.fixture
def bus(run, private_bus):
    connection = Bus(run, private_bus)
    yield connection
    connection.close()


@pytest.fixture
def portal_manager(bus, wmanager):
    """Qtile, with its portal owning its name on the private bus."""

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_portal():
        assert bus.call("NameHasOwner", BUS_NAME, signature="s") == [True]

    wait_for_portal()
    return wmanager


@pytest.fixture
def virtual_pointer(wmanager):
    with ClientHandler("virtual-pointer", wmanager) as pointer:
        yield pointer


def right_barrier(width, height, x, y, barrier_id=1):
    return {
        "barrier_id": Variant("u", barrier_id),
        "position": Variant("(iiii)", [x + width, y, x + width, y + height - 1]),
    }


def wait_for_devices(receiver):
    @Retry(ignore_exceptions=(AssertionError,))
    def wait():
        status = receiver.lines("status")
        assert "pointer: resumed" in status, status
        assert "keyboard: resumed" in status, status

    wait()


def session_count(wmanager):
    return wmanager.c.eval("len(self.core.input_capture_sessions)")


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_input_capture(portal_manager, bus, run, virtual_keyboard, virtual_pointer):
    """An allowed program gets a session, an EIS connection and capture signals"""
    wmanager = portal_manager
    width, height, x, y = zone(wmanager)
    virtual_keyboard.assert_ok("clear_modifiers")

    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    assert run(ic.get_version()) == 2
    assert run(ic.get_supported_capabilities()) == CAPABILITIES

    activated = []
    ic.on_activated(lambda handle, options: activated.append((handle, options)))

    session = f"/org/freedesktop/portal/desktop/session/{bus.sender}/t1"
    assert run(ic.call_create_session2(session, "", {})) == {}
    response, results = run(
        ic.call_start("/request/1", session, "", "", {"capabilities": Variant("u", CAPABILITIES)})
    )
    assert response == 0
    assert results["capabilities"].value == CAPABILITIES

    response, results = run(ic.call_get_zones("/request/2", session, "", {}))
    assert response == 0
    assert results["zones"].value == [[width, height, x, y]]
    zone_set = results["zone_set"].value

    # Barriers for another zone set all fail; of the rest, only valid ones are accepted
    barrier = right_barrier(width, height, x, y)
    middle = {
        "barrier_id": Variant("u", 2),
        "position": Variant("(iiii)", [x + width // 2, y, x + width // 2, y + height - 1]),
    }
    _, results = run(
        ic.call_set_pointer_barriers("/request/3", session, "", {}, [barrier], zone_set + 1)
    )
    assert results["failed_barriers"].value == [1]
    _, results = run(
        ic.call_set_pointer_barriers("/request/4", session, "", {}, [barrier, middle], zone_set)
    )
    assert results["failed_barriers"].value == [2]

    fd = run(ic.call_connect_to_eis(session, "", {}))
    with EIReceiver(wmanager, fd) as receiver:
        wait_for_devices(receiver)

        assert run(ic.call_enable(session, "", {}))[0] == 0
        wmanager.c.eval(f"self.core.warp_pointer({width - 10}, {height // 2})")
        virtual_pointer.assert_ok("motion 20 0")

        bus.wait_until(lambda: activated)
        handle, options = activated[0]
        assert handle == session
        assert options["activation_id"].value == 1
        assert options["barrier_id"].value == 1
        assert options["cursor_position"].value[0] >= width

        release = {
            "activation_id": Variant("u", 1),
            "cursor_position": Variant("(dd)", [width / 2, height / 2]),
        }
        assert run(ic.call_release(session, "", release))[0] == 0
        assert wmanager.c.eval("repr(self.core.qw_cursor.cursor.x)") == repr(width / 2)

    # Closing the session ends it in qtile
    session_object = bus.interface(BUS_NAME, session, "org.freedesktop.impl.portal.Session")
    run(session_object.call_close())
    response, _ = run(ic.call_get_zones("/request/5", session, "", {}))
    assert response == 2
    assert session_count(wmanager) == "0"


@pytest.mark.parametrize("wmanager", [RefusingPortalConfig], indirect=True)
def test_portal_refuses_other_programs(portal_manager, bus, run):
    """Programs that are not allowed cannot start a session"""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    session = f"/org/freedesktop/portal/desktop/session/{bus.sender}/t1"
    run(ic.call_create_session2(session, "", {}))
    response, _ = run(
        ic.call_start("/request/1", session, "", "", {"capabilities": Variant("u", CAPABILITIES)})
    )
    assert response == 2
    response, _ = run(ic.call_get_zones("/request/2", session, "", {}))
    assert response == 2
    assert session_count(portal_manager) == "0"


@pytest.fixture
def portal_frontend(portal_manager, private_bus):
    """xdg-desktop-portal on the private bus, routing InputCapture to qtile."""
    frontend = Path("/usr/libexec/xdg-desktop-portal")
    if not frontend.exists():
        pytest.skip("xdg-desktop-portal is not installed")

    with tempfile.TemporaryDirectory() as tmp:
        portals = Path(tmp) / "portals"
        portals.mkdir()
        shutil.copy(PORTAL_FILE, portals)
        # With XDG_DESKTOP_PORTAL_DIR set, the configuration is read from there too
        (portals / "qtile-portals.conf").write_text(
            "[preferred]\ndefault=none\n"
            "org.freedesktop.impl.portal.InputCapture=qtile\n"
            "org.freedesktop.impl.portal.Clipboard=qtile\n"
        )
        runtime = Path(tmp) / "runtime"
        runtime.mkdir(mode=0o700)
        env = dict(
            os.environ,
            DBUS_SESSION_BUS_ADDRESS=private_bus,
            XDG_DESKTOP_PORTAL_DIR=portals.as_posix(),
            XDG_CURRENT_DESKTOP="qtile",
            # Anything it creates stays out of the user's runtime directory
            XDG_RUNTIME_DIR=runtime.as_posix(),
        )
        process = subprocess.Popen(
            [frontend.as_posix(), "--replace"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        yield
        process.terminate()
        process.wait()


def frontend_interface(bus, interface):
    @Retry(ignore_exceptions=(Exception,))
    def get():
        return bus.interface("org.freedesktop.portal.Desktop", OBJECT_PATH, interface)

    return get()


def request(bus, run, method, *args, token):
    """Call a portal method that answers through a Request object."""
    path = f"/org/freedesktop/portal/desktop/request/{bus.sender}/{token}"
    responses = []

    def handler(message):
        if message.path == path and message.member == "Response":
            responses.append(message.body)

    bus.bus.add_message_handler(handler)
    bus.call("AddMatch", f"type='signal',path='{path}'", signature="s")
    run(method(*args))
    bus.wait_until(lambda: responses)
    return responses[0]


def start_frontend_capture(bus, run, ic, token, clipboard=None):
    """
    Create, start and aim a capture session through the frontend, as Synergy does. Asks
    for the clipboard first if a Clipboard proxy is given. Returns the session handle, the
    Start results and the EIS fd.
    """
    results = run(ic.call_create_session2({"session_handle_token": Variant("s", f"{token}s")}))
    session = results["session_handle"].value
    if clipboard is not None:
        run(clipboard.call_request_clipboard(session, {}))

    options = {
        "handle_token": Variant("s", f"{token}1"),
        "capabilities": Variant("u", CAPABILITIES),
    }
    response, start_results = request(
        bus, run, ic.call_start, session, "", options, token=f"{token}1"
    )
    assert response == 0

    options = {"handle_token": Variant("s", f"{token}2")}
    response, results = request(bus, run, ic.call_get_zones, session, options, token=f"{token}2")
    assert response == 0
    [[width, height, x, y]] = results["zones"].value

    options = {"handle_token": Variant("s", f"{token}3")}
    barriers = [right_barrier(width, height, x, y)]
    zone_set = results["zone_set"].value
    response, results = request(
        bus,
        run,
        ic.call_set_pointer_barriers,
        session,
        options,
        barriers,
        zone_set,
        token=f"{token}3",
    )
    assert response == 0
    assert results["failed_barriers"].value == []

    return session, start_results, run(ic.call_connect_to_eis(session, {}))


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_through_frontend(portal_frontend, portal_manager, bus, run, virtual_keyboard):
    """Through xdg-desktop-portal, as libportal (and so Synergy) uses it"""
    virtual_keyboard.assert_ok("clear_modifiers")

    ic = frontend_interface(bus, "org.freedesktop.portal.InputCapture")
    assert run(ic.get_version()) >= 2

    _, _, fd = start_frontend_capture(bus, run, ic, "r")
    with EIReceiver(portal_manager, fd) as receiver:
        wait_for_devices(receiver)


def clipboard_interface(bus):
    return bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.Clipboard")


def start_session(bus, run, ic, token, *, request_clipboard=False):
    """Create and start an input capture session, optionally asking for the clipboard first."""
    session = f"/org/freedesktop/portal/desktop/session/{bus.sender}/{token}"
    run(ic.call_create_session2(session, "", {}))
    if request_clipboard:
        run(clipboard_interface(bus).call_request_clipboard(session, {}))
    response, results = run(
        ic.call_start(
            f"/request/{token}", session, "", "", {"capabilities": Variant("u", CAPABILITIES)}
        )
    )
    return session, response, results


def assert_access_denied(run, call):
    with pytest.raises(DBusError) as error:
        run(call)
    assert error.value.type == ErrorType.ACCESS_DENIED


def assert_failed(run, call):
    with pytest.raises(DBusError) as error:
        run(call)
    assert error.value.type == ErrorType.FAILED


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_interface(portal_manager, bus, run):
    """The Clipboard interface is exported beside InputCapture, at version 1"""
    assert run(clipboard_interface(bus).get_version()) == 1


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_enabling(portal_manager, bus, run):
    """RequestClipboard before Start enables the clipboard; a session that did not ask does not"""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    _, response, results = start_session(bus, run, ic, "asked", request_clipboard=True)
    assert response == 0
    assert results["clipboard_enabled"].value is True

    _, response, results = start_session(bus, run, ic, "plain")
    assert response == 0
    assert "clipboard_enabled" not in results


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_needs_enabling(portal_manager, bus, run):
    """Clipboard calls are refused unless the clipboard was enabled at Start"""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    clipboard = clipboard_interface(bus)
    session, response, _ = start_session(bus, run, ic, "plain")
    assert response == 0

    # Asking after the session started does not enable it
    run(clipboard.call_request_clipboard(session, {}))
    selection = {"mime_types": Variant("as", ["text/plain"])}
    assert_access_denied(run, clipboard.call_set_selection(session, selection))
    assert_access_denied(run, clipboard.call_selection_read(session, "text/plain"))
    assert_access_denied(run, clipboard.call_selection_write(session, 1))
    assert_access_denied(run, clipboard.call_selection_write_done(session, 1, True))


@pytest.mark.parametrize("wmanager", [RefusingPortalConfig], indirect=True)
def test_portal_clipboard_refused(portal_manager, bus, run):
    """A refused program's session never starts, so it never gets the clipboard"""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    session, response, _ = start_session(bus, run, ic, "refused", request_clipboard=True)
    assert response == 2
    selection = {"mime_types": Variant("as", ["text/plain"])}
    assert_access_denied(run, clipboard_interface(bus).call_set_selection(session, selection))


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_unknown_data(portal_manager, bus, run):
    """With no selection to read and no transfer under a serial, the calls fail"""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    clipboard = clipboard_interface(bus)
    session, response, _ = start_session(bus, run, ic, "asked", request_clipboard=True)
    assert response == 0

    assert_failed(run, clipboard.call_selection_read(session, "text/plain"))
    assert_failed(run, clipboard.call_selection_write(session, 7))
    assert_failed(run, clipboard.call_selection_write_done(session, 7, True))


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_through_frontend(portal_frontend, portal_manager, bus, run):
    """Through xdg-desktop-portal, RequestClipboard before Start enables the clipboard"""
    ic = frontend_interface(bus, "org.freedesktop.portal.InputCapture")
    clipboard = frontend_interface(bus, "org.freedesktop.portal.Clipboard")
    assert run(clipboard.get_version()) >= 1

    results = run(ic.call_create_session2({"session_handle_token": Variant("s", "c1")}))
    session = results["session_handle"].value
    run(clipboard.call_request_clipboard(session, {}))

    options = {"handle_token": Variant("s", "c2"), "capabilities": Variant("u", CAPABILITIES)}
    response, results = request(bus, run, ic.call_start, session, "", options, token="c2")
    assert response == 0
    assert results["clipboard_enabled"].value is True


def record_signals(clipboard):
    """Collect the Clipboard signals of a proxy: owner changes as dicts, and transfers."""
    signals = {"owner": [], "transfer": []}
    clipboard.on_selection_owner_changed(
        lambda handle, options: signals["owner"].append({k: v.value for k, v in options.items()})
    )
    clipboard.on_selection_transfer(
        lambda handle, mime_type, serial: signals["transfer"].append((mime_type, serial))
    )
    return signals


def start_paste(wmanager, mime_type="text/plain"):
    """Start wl-paste without waiting, so the test can answer the transfer it causes."""
    return subprocess.Popen(
        ["wl-paste", "--no-newline", "--type", mime_type],
        env=make_test_env(wmanager),
        stdout=subprocess.PIPE,
    )


def answer_paste(bus, run, clipboard, session, signals, proc, payload):
    """Serve a paste the way Synergy does: write the payload, then report success."""
    bus.wait_until(lambda: signals["transfer"])
    mime_type, serial = signals["transfer"].pop(0)
    assert mime_type == "text/plain"
    fd = run(clipboard.call_selection_write(session, serial))
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    run(clipboard.call_selection_write_done(session, serial, True))
    stdout, _ = proc.communicate(timeout=10)
    return stdout


def set_portal_constant(wmanager, name, value):
    wmanager.c.eval(f"{CLIPBOARD_HELPER}.set_portal_constant({name!r}, {value!r})")


def started_clipboard_session(bus, run, token):
    """An allowed session that asked for the clipboard before it started."""
    ic = bus.interface(BUS_NAME, OBJECT_PATH, "org.freedesktop.impl.portal.InputCapture")
    session, response, _ = start_session(bus, run, ic, token, request_clipboard=True)
    assert response == 0
    return session


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_client_copy_is_read(portal_manager, bus, run, copy):
    """A client's copy is announced to the session, and SelectionRead returns its text"""
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "reader")

    copy("hello")
    bus.wait_until(lambda: any("text/plain" in o["mime_types"] for o in signals["owner"]))
    announced = [o for o in signals["owner"] if "text/plain" in o["mime_types"]]
    assert announced[-1]["session_is_owner"] is False

    fd = run(clipboard.call_selection_read(session, "text/plain"))
    with os.fdopen(fd, "rb") as f:
        assert f.read() == b"hello"


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_set_selection_is_pasted(portal_manager, bus, run):
    """SetSelection makes the session the owner, and a paste gets the payload it writes"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "source")

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))
    assert set(signals["owner"][-1]["mime_types"]) == set(MIME_TYPES)
    assert signals["owner"][-1]["session_is_owner"] is True

    proc = start_paste(wmanager)
    assert answer_paste(bus, run, clipboard, session, signals, proc, b"payload") == b"payload"


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_empty_set_selection_keeps_clipboard(portal_manager, bus, run):
    """An empty SetSelection clears the source only; the clipboard stays enabled"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "clear")

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", [])}))
    bus.wait_until(lambda: signals["owner"][-1]["mime_types"] == [])
    # Still enabled: with no selection the read fails, rather than being refused
    assert_failed(run, clipboard.call_selection_read(session, "text/plain"))

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: signals["owner"][-1]["session_is_owner"])
    proc = start_paste(wmanager)
    assert answer_paste(bus, run, clipboard, session, signals, proc, b"again") == b"again"


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_unanswered_transfer_times_out(portal_manager, bus, run):
    """An unanswered paste ends at TRANSFER_TIMEOUT, and the portal's fd is closed"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "silent")
    set_portal_constant(wmanager, "TRANSFER_TIMEOUT", 0.5)
    before = settled_fd_count(wmanager)

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    proc = start_paste(wmanager)
    bus.wait_until(lambda: signals["transfer"])
    started = time.monotonic()
    # Without the timer this waits forever on the portal's fd
    stdout, _ = proc.communicate(timeout=10)
    assert time.monotonic() - started < 5
    assert stdout == b""
    assert settled_fd_count(wmanager) == before


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_close_clears_selection(portal_manager, bus, run):
    """Closing a session that owns the selection clears it"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "owner")

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))
    assert list_types(wmanager).returncode == 0

    session_object = bus.interface(BUS_NAME, session, "org.freedesktop.impl.portal.Session")
    run(session_object.call_close())
    assert list_types(wmanager).returncode != 0


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_close_closes_pending_transfer(portal_manager, bus, run):
    """Closing a session during a paste closes the transfer fd it holds"""
    wmanager = portal_manager
    # A started session holds fds of its own, so the count is compared with the one before it
    idle = settled_fd_count(wmanager)
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "pending")

    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))
    before = settled_fd_count(wmanager)

    proc = start_paste(wmanager)
    bus.wait_until(lambda: signals["transfer"])
    assert settled_fd_count(wmanager) > before

    session_object = bus.interface(BUS_NAME, session, "org.freedesktop.impl.portal.Session")
    run(session_object.call_close())
    # wl-paste ends only at end-of-file, which needs the portal's copy of the write end closed
    assert proc.communicate(timeout=10)[0] == b""
    assert settled_fd_count(wmanager) == idle


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_close_closes_returned_read(portal_manager, bus, run, copy):
    """Closing a session closes the read fds SelectionRead returned and the portal kept"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    copy("held")
    idle = settled_fd_count(wmanager)
    session = started_clipboard_session(bus, run, "reader")
    before = settled_fd_count(wmanager)

    fd = run(clipboard.call_selection_read(session, "text/plain"))
    with os.fdopen(fd, "rb") as f:
        assert f.read() == b"held"
    # The portal keeps its own copy of the read end until the linger ends or the session closes
    assert settled_fd_count(wmanager) == before + 1

    session_object = bus.interface(BUS_NAME, session, "org.freedesktop.impl.portal.Session")
    run(session_object.call_close())
    assert settled_fd_count(wmanager) == idle


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_no_fd_leak(portal_manager, bus, run, copy):
    """Repeated reads and transfers leave no fd open once their lingers have ended"""
    wmanager = portal_manager
    clipboard = clipboard_interface(bus)
    signals = record_signals(clipboard)
    session = started_clipboard_session(bus, run, "leak")
    set_portal_constant(wmanager, "RETURNED_FD_LINGER", 0.5)

    copy("client")
    before = settled_fd_count(wmanager)
    for _ in range(10):
        fd = run(clipboard.call_selection_read(session, "text/plain"))
        with os.fdopen(fd, "rb") as f:
            assert f.read() == b"client"
    bus.wait_until(lambda: settled_fd_count(wmanager) == before)

    # Replacing the wl-copy selection ends wl-copy, so the transfers are measured from here
    run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
    bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))
    before = settled_fd_count(wmanager)
    for _ in range(10):
        proc = start_paste(wmanager)
        assert answer_paste(bus, run, clipboard, session, signals, proc, b"payload") == b"payload"
    assert settled_fd_count(wmanager) == before


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_clipboard_through_frontend_data(
    portal_frontend, portal_manager, bus, run, virtual_pointer, copy
):
    """Through xdg-desktop-portal, an active session reads and supplies the clipboard"""
    wmanager = portal_manager
    width, height, x, y = zone(wmanager)
    ic = frontend_interface(bus, "org.freedesktop.portal.InputCapture")
    clipboard = frontend_interface(bus, "org.freedesktop.portal.Clipboard")
    signals = record_signals(clipboard)
    activated = []
    ic.on_activated(lambda handle, options: activated.append(handle))

    session, start_results, fd = start_frontend_capture(bus, run, ic, "d", clipboard)
    assert start_results["clipboard_enabled"].value is True
    with EIReceiver(wmanager, fd) as receiver:
        wait_for_devices(receiver)
        run(ic.call_enable(session, {}))
        wmanager.c.eval(f"self.core.warp_pointer({width - 10}, {height // 2})")
        virtual_pointer.assert_ok("motion 20 0")
        bus.wait_until(lambda: activated)

        # While the capture is active, a Fedora copy is read for the Mac
        copy("from fedora")
        bus.wait_until(lambda: any("text/plain" in o["mime_types"] for o in signals["owner"]))
        fd = run(clipboard.call_selection_read(session, "text/plain"))
        with os.fdopen(fd, "rb") as f:
            assert f.read() == b"from fedora"

        # A Mac selection is served to a Fedora paste through the frontend
        run(clipboard.call_set_selection(session, {"mime_types": Variant("as", MIME_TYPES)}))
        bus.wait_until(lambda: any(o["session_is_owner"] for o in signals["owner"]))
        proc = start_paste(wmanager)
        assert (
            answer_paste(bus, run, clipboard, session, signals, proc, b"from mac") == b"from mac"
        )
