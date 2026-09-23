import ast
import fcntl
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from libqtile.config import Key
from libqtile.lazy import lazy
from test.backend.wayland.conftest import ClientHandler, make_test_env
from test.helpers import BareConfig, Retry


class InputCaptureConfig(BareConfig):
    keys = [Key(["control"], key, lazy.group[key].toscreen()) for key in "abcd"]


KEY_ESC = 1
KEY_A = 30
KEY_LEFTSHIFT = 42
BTN_LEFT = 0x110


pytestmark = [
    pytest.mark.parametrize("wmanager", [InputCaptureConfig], indirect=True),
]


class EIReceiver(ClientHandler):
    """The ei-receiver test client, handed the client end of the session's EIS connection."""

    def __init__(self, manager, fd):
        super().__init__("ei-receiver", manager)
        self.fd = fd

    def _run(self):
        self.process = subprocess.Popen(
            [self.cmd, str(self.fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=make_test_env(self.manager),
            pass_fds=(self.fd,),
        )
        fcntl.fcntl(self.process.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
        os.close(self.fd)

    def lines(self, command):
        lines = self.send_read_until(command, "OK")
        return [line for line in lines if line != "OK"]


def create_session(wmanager):
    """Create a session in qtile and return the client end of its EIS connection."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fd.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path.as_posix())
        listener.listen(1)
        # exec() does not give lambdas access to its locals, hence the default arguments
        wmanager.c.eval(
            f"""
import os, socket
from libqtile.backend.wayland.input_capture import InputCaptureSession
events = self.core._test_capture_events = []
session = self.core._test_capture = InputCaptureSession(
    self.core,
    on_activated=lambda *a, ev=events: ev.append(("activated",) + a),
    on_deactivated=lambda i, ev=events: ev.append(("deactivated", i)),
    on_disabled=lambda ev=events: ev.append(("disabled",)),
    on_zones_changed=lambda ev=events: ev.append(("zones_changed",)),
)
fd = session.connect_eis()
conn = socket.socket(socket.AF_UNIX)
conn.connect({path.as_posix()!r})
socket.send_fds(conn, [b"x"], [fd])
conn.close()
os.close(fd)
"""
        )
        conn, _ = listener.accept()
        _, fds, _, _ = socket.recv_fds(conn, 1, 1)
        conn.close()
        listener.close()
    return fds[0]


@pytest.fixture
def virtual_pointer(wmanager):
    with ClientHandler("virtual-pointer", wmanager) as pointer:
        yield pointer


@pytest.fixture
def receiver(wmanager, virtual_keyboard):
    # The virtual keyboard gives the seat a keyboard with a keymap for the EIS keyboard
    virtual_keyboard.assert_ok("clear_modifiers")
    with EIReceiver(wmanager, create_session(wmanager)) as receiver:
        yield receiver


def capture_events(wmanager):
    return ast.literal_eval(wmanager.c.eval("repr(self.core._test_capture_events)"))


def clear_capture_events(wmanager):
    wmanager.c.eval("self.core._test_capture_events.clear()")


def session_call(wmanager, call):
    return wmanager.c.eval(f"self.core._test_capture.{call}")


def cursor_position(wmanager):
    return ast.literal_eval(
        wmanager.c.eval("repr((self.core.qw_cursor.cursor.x, self.core.qw_cursor.cursor.y))")
    )


IC_MODULE = "__import__('libqtile.backend.wayland.input_capture', fromlist=['get_zones'])"


def zone(wmanager):
    zones = ast.literal_eval(
        wmanager.c.eval(
            f"repr([(z.width, z.height, z.x, z.y) for z in {IC_MODULE}.get_zones(self.core)])"
        )
    )
    return zones[0]


def set_barriers(wmanager, barriers):
    """Set barriers given as (id, x1, y1, x2, y2), returning the IDs of those rejected."""
    specs = ", ".join(f"ic.Barrier{barrier}" for barrier in barriers)
    # eval() does not give lambdas access to its locals, so self is passed in
    return ast.literal_eval(
        wmanager.c.eval(
            f"(lambda ic, s: repr(s.core._test_capture.set_barriers([{specs}])))({IC_MODULE}, self)"
        )
    )


def set_right_barrier(wmanager):
    width, height, x, y = zone(wmanager)
    assert set_barriers(wmanager, [(1, x + width, y, x + width, y + height - 1)]) == []
    session_call(wmanager, "enable()")
    return width, height


def activate(wmanager, virtual_pointer, width, height):
    """Push the pointer across the right-hand barrier."""
    wmanager.c.eval(f"self.core.warp_pointer({width - 10}, {height // 2})")
    virtual_pointer.assert_ok("motion 5 0")
    assert capture_events(wmanager) == []
    virtual_pointer.assert_ok("motion 20 0")

    @Retry(ignore_exceptions=(AssertionError, IndexError))
    def wait_for_activation():
        event = capture_events(wmanager)[-1]
        assert event[0] == "activated"
        return event

    return wait_for_activation()


def tap_with_control(virtual_keyboard, key):
    virtual_keyboard.assert_ok("set_modifier control")
    virtual_keyboard.assert_ok(f"tap {key}")
    virtual_keyboard.assert_ok("clear_modifiers")


def wait_for_receiver_events(receiver, expected):
    """Wait until the receiver has seen the expected events, in order."""
    seen = []

    @Retry(ignore_exceptions=(AssertionError,))
    def wait():
        seen.extend(receiver.lines("events"))
        assert seen == expected, seen

    wait()


def test_input_capture_devices(wmanager, receiver):
    """The receiver gets a pointer with the zones as regions and a keyboard with a keymap"""
    width, height, x, y = zone(wmanager)

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_devices():
        status = receiver.lines("status")
        assert "pointer: resumed" in status, status
        assert "keyboard: resumed" in status, status
        assert "keymap: yes" in status, status
        assert f"region: {width}x{height}+{x}+{y}" in status, status

    wait_for_devices()


def test_input_capture_barriers(wmanager, receiver):
    """Only barriers on the outer edge of the zones are accepted"""
    width, height, x, y = zone(wmanager)
    barriers = [
        (1, x + width, y, x + width, y + height - 1),  # right edge
        (2, x, y, x + width - 1, y),  # top edge
        (3, x + width // 2, y, x + width // 2, y + height - 1),  # middle
        (4, x + width, y, x + width, y + height + 10),  # longer than the zone
        (5, x, y, x + 10, y + 10),  # diagonal
    ]
    assert set_barriers(wmanager, barriers) == [3, 4, 5]


def test_input_capture(wmanager, receiver, virtual_pointer, virtual_keyboard):
    """Crossing a barrier sends input to the receiver until the session releases it"""
    width, height = set_right_barrier(wmanager)

    # Crossing the right edge activates the capture with the unclamped position
    event = activate(wmanager, virtual_pointer, width, height)
    _, activation_id, barrier_id, x, y = event
    assert activation_id == 1
    assert barrier_id == 1
    assert x >= width
    wait_for_receiver_events(receiver, ["start pointer 1", "start keyboard 1", "motion 20 0"])
    stuck_at = cursor_position(wmanager)

    # Pointer input goes to the receiver and the cursor stays put
    virtual_pointer.assert_ok("motion 3 4")
    virtual_pointer.assert_ok(f"press {BTN_LEFT}")
    virtual_pointer.assert_ok(f"release {BTN_LEFT}")
    virtual_pointer.assert_ok("wheel 1")
    wait_for_receiver_events(
        receiver,
        [
            "motion 3 4",
            f"button {BTN_LEFT} press",
            f"button {BTN_LEFT} release",
            "scroll_discrete 0 120",
        ],
    )
    assert cursor_position(wmanager) == stuck_at

    # So do key bindings, which qtile does not act on
    assert wmanager.c.group.info()["name"] == "a"
    tap_with_control(virtual_keyboard, KEY_A)
    wait_for_receiver_events(receiver, [f"key {KEY_A} press", f"key {KEY_A} release"])
    assert wmanager.c.group.info()["name"] == "a"

    # A stale release is ignored
    session_call(wmanager, "release(0)")
    virtual_pointer.assert_ok("motion 1 1")
    wait_for_receiver_events(receiver, ["motion 1 1"])

    # Releasing warps the cursor and hands input back to qtile, without a signal
    session_call(wmanager, f"release({activation_id}, ({width // 2}, {height // 2}))")
    wait_for_receiver_events(receiver, ["stop keyboard", "stop pointer"])
    assert cursor_position(wmanager) == (width // 2, height // 2)
    assert capture_events(wmanager) == [event]
    tap_with_control(virtual_keyboard, KEY_A)
    assert wmanager.c.group.info()["name"] == "a"
    tap_with_control(virtual_keyboard, 48)  # b
    assert wmanager.c.group.info()["name"] == "b"
    assert receiver.lines("events") == []


def test_input_capture_release_key(wmanager, receiver, virtual_pointer, virtual_keyboard):
    """The release key always ends a capture and disables the session"""
    width, height = set_right_barrier(wmanager)
    activate(wmanager, virtual_pointer, width, height)
    receiver.lines("events")
    clear_capture_events(wmanager)

    virtual_keyboard.assert_ok("set_modifier super")
    virtual_keyboard.assert_ok("set_modifier shift")
    virtual_keyboard.assert_ok(f"tap {KEY_ESC}")
    virtual_keyboard.assert_ok("clear_modifiers")

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_release():
        assert capture_events(wmanager) == [("deactivated", 1), ("disabled",)]

    wait_for_release()
    # The escape key itself never reaches the receiver
    assert f"key {KEY_ESC} press" not in receiver.lines("events")

    # Disabled, so crossing the barrier again does nothing until re-enabled
    clear_capture_events(wmanager)
    wmanager.c.eval(f"self.core.warp_pointer({width - 10}, {height // 2})")
    virtual_pointer.assert_ok("motion 20 0")
    assert capture_events(wmanager) == []
    assert cursor_position(wmanager)[0] > width - 10

    session_call(wmanager, "enable()")
    event = activate(wmanager, virtual_pointer, width, height)
    assert event[1] == 2


def test_input_capture_receiver_disconnect(wmanager, receiver, virtual_pointer, virtual_keyboard):
    """Input comes back to qtile if the receiver goes away mid-capture"""
    width, height = set_right_barrier(wmanager)
    activate(wmanager, virtual_pointer, width, height)
    clear_capture_events(wmanager)

    receiver.stop()

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_release():
        assert capture_events(wmanager) == [("deactivated", 1), ("disabled",)]

    wait_for_release()
    tap_with_control(virtual_keyboard, 48)  # b
    assert wmanager.c.group.info()["name"] == "b"


def test_input_capture_key_repeat(wmanager, receiver, virtual_pointer, virtual_keyboard):
    """Held keys repeat as presses, which receivers take as repeats; modifiers do not"""
    width, height = set_right_barrier(wmanager)
    activate(wmanager, virtual_pointer, width, height)
    receiver.lines("events")

    # wlroots keyboards repeat after 600ms at 25Hz by default
    virtual_keyboard.assert_ok(f"press {KEY_A}")
    time.sleep(1)
    virtual_keyboard.assert_ok(f"release {KEY_A}")
    virtual_keyboard.assert_ok(f"press {KEY_LEFTSHIFT}")
    time.sleep(1)
    virtual_keyboard.assert_ok(f"release {KEY_LEFTSHIFT}")
    time.sleep(0.2)

    events = receiver.lines("events")
    a_events = [e for e in events if e.startswith(f"key {KEY_A} ")]
    assert a_events[-1] == f"key {KEY_A} release"
    assert set(a_events[:-1]) == {f"key {KEY_A} press"}
    assert len(a_events[:-1]) >= 5, events
    assert [e for e in events if e.startswith(f"key {KEY_LEFTSHIFT} ")] == [
        f"key {KEY_LEFTSHIFT} press",
        f"key {KEY_LEFTSHIFT} release",
    ]
