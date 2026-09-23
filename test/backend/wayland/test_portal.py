import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus

from libqtile.config import Key
from libqtile.lazy import lazy
from test.backend.wayland.conftest import ClientHandler
from test.backend.wayland.test_input_capture import EIReceiver, zone
from test.helpers import BareConfig, Retry

BUS_NAME = "org.freedesktop.impl.portal.desktop.qtile"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_FILE = Path(__file__).parents[3] / "libqtile" / "resources" / "portal" / "qtile.portal"

# The tests talk to the portal from this process, which the allowlist sees by executable
TEST_EXECUTABLE = os.readlink("/proc/self/exe")
CAPABILITIES = 3  # keyboard and pointer


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
            "[preferred]\ndefault=none\norg.freedesktop.impl.portal.InputCapture=qtile\n"
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


@pytest.mark.parametrize("wmanager", [PortalConfig], indirect=True)
def test_portal_through_frontend(portal_frontend, portal_manager, bus, run, virtual_keyboard):
    """Through xdg-desktop-portal, as libportal (and so Synergy) uses it"""
    virtual_keyboard.assert_ok("clear_modifiers")

    @Retry(ignore_exceptions=(Exception,))
    def frontend_interface():
        return bus.interface(
            "org.freedesktop.portal.Desktop", OBJECT_PATH, "org.freedesktop.portal.InputCapture"
        )

    ic = frontend_interface()
    assert run(ic.get_version()) >= 2

    def request(method, *args, token):
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

    results = run(ic.call_create_session2({"session_handle_token": Variant("s", "s1")}))
    session = results["session_handle"].value

    options = {"handle_token": Variant("s", "r1"), "capabilities": Variant("u", CAPABILITIES)}
    response, _ = request(ic.call_start, session, "", options, token="r1")
    assert response == 0

    options = {"handle_token": Variant("s", "r2")}
    response, results = request(ic.call_get_zones, session, options, token="r2")
    assert response == 0
    [[width, height, x, y]] = results["zones"].value

    options = {"handle_token": Variant("s", "r3")}
    barriers = [right_barrier(width, height, x, y)]
    zone_set = results["zone_set"].value
    response, results = request(
        ic.call_set_pointer_barriers, session, options, barriers, zone_set, token="r3"
    )
    assert response == 0
    assert results["failed_barriers"].value == []

    fd = run(ic.call_connect_to_eis(session, {}))
    with EIReceiver(portal_manager, fd) as receiver:
        wait_for_devices(receiver)
