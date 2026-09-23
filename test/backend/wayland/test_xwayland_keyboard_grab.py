import shutil

import pytest
import xcffib
import xcffib.xproto
from xcffib.xproto import CW, EventMask, GrabMode, GrabStatus, WindowClass

from libqtile.config import Key
from libqtile.lazy import lazy
from test.helpers import BareConfig, Retry


class XwaylandKeyboardGrabConfig(BareConfig):
    keys = [Key(["control"], key, lazy.group[key].toscreen()) for key in "abcd"]


KEY_A = 30
KEY_B = 48
X_KEYCODE_OFFSET = 8
MOD_CONTROL = 4


pytestmark = [
    pytest.mark.skipif(shutil.which("Xwayland") is None, reason="Xwayland is not installed"),
    pytest.mark.parametrize("wmanager", [XwaylandKeyboardGrabConfig], indirect=True),
]


@pytest.fixture
def x11_window(wmanager):
    """An X11 window, running under Xwayland, that records key presses."""
    display = wmanager.backend.env["DISPLAY"]
    if not display:
        pytest.skip("Qtile was built without Xwayland")

    conn = xcffib.connect(display=display)
    screen = conn.get_setup().roots[conn.pref_screen]
    wid = conn.generate_id()
    conn.core.CreateWindow(
        screen.root_depth,
        wid,
        screen.root,
        0,
        0,
        100,
        100,
        0,
        WindowClass.InputOutput,
        screen.root_visual,
        # Xwayland only commits a buffer, and so maps the window, once it has content
        CW.BackPixel | CW.EventMask,
        [screen.white_pixel, EventMask.KeyPress],
    )
    conn.core.MapWindow(wid)
    conn.flush()

    yield conn, wid

    conn.disconnect()


def get_keypresses(conn):
    """Return the (evdev keycode, modifier state) of each key press received so far."""
    presses = []
    while event := conn.poll_for_event():
        if isinstance(event, xcffib.xproto.KeyPressEvent):
            presses.append((event.detail - X_KEYCODE_OFFSET, event.state))
    return presses


def press_with_control(virtual_keyboard, key):
    virtual_keyboard.assert_ok("set_modifier control")
    virtual_keyboard.assert_ok(f"tap {key}")
    virtual_keyboard.assert_ok("clear_modifiers")


def test_xwayland_keyboard_grab(wmanager, virtual_keyboard, x11_window):
    """Test that an X11 client can grab all keypresses"""
    conn, wid = x11_window

    def assert_group(name):
        assert wmanager.c.group.info()["name"] == name

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_windows(count):
        assert len(wmanager.c.windows()) == count

    wait_for_windows(1)
    wmanager.c.window.togroup("b")
    wmanager.c.group["b"].toscreen()
    assert_group("b")

    # Without a grab qtile handles the key binding and the client sees nothing
    press_with_control(virtual_keyboard, KEY_A)
    assert_group("a")
    wmanager.c.group["b"].toscreen()  # Window needs to be focused
    assert get_keypresses(conn) == []

    # Grab the keyboard as a remote desktop client in immersive mode would
    reply = conn.core.GrabKeyboard(
        False, wid, xcffib.CurrentTime, GrabMode.Async, GrabMode.Async
    ).reply()
    assert reply.status == GrabStatus.Success

    # Xwayland forwards the grab asynchronously. Ctrl+B is harmless while group b is
    # already shown, so retry it until the client receives it.
    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_grab():
        press_with_control(virtual_keyboard, KEY_B)
        assert (KEY_B, MOD_CONTROL) in get_keypresses(conn)

    wait_for_grab()

    # Bound keys now reach the client and qtile does not change groups
    press_with_control(virtual_keyboard, KEY_A)

    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_keypress(expected):
        assert get_keypresses(conn) == [expected]

    wait_for_keypress((KEY_A, MOD_CONTROL))
    assert_group("b")

    # Release the grab and confirm that qtile swallows keys again
    conn.core.UngrabKeyboard(xcffib.CurrentTime)
    conn.flush()

    # Until the release reaches qtile, Ctrl+A just goes to the client, so retry it
    @Retry(ignore_exceptions=(AssertionError,))
    def wait_for_ungrab():
        press_with_control(virtual_keyboard, KEY_A)
        assert_group("a")

    wait_for_ungrab()
