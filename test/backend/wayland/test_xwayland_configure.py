import shutil
import struct
import time

import pytest
import xcffib
import xcffib.xproto
from xcffib.xproto import CW, Atom, ConfigWindow, EventMask, PropMode, WindowClass

from test.conftest import BareConfig
from test.helpers import Retry, window_by_name

pytestmark = [
    pytest.mark.skipif(shutil.which("Xwayland") is None, reason="Xwayland is not installed"),
    pytest.mark.parametrize("wmanager", [BareConfig], indirect=True),
]

NAME = "xwayland-configure"
P_MIN_SIZE = 1 << 4
P_MAX_SIZE = 1 << 5


@pytest.fixture
def x11_conn(wmanager):
    display = wmanager.backend.env["DISPLAY"]
    if not display:
        pytest.skip("Qtile was built without Xwayland")

    conn = xcffib.connect(display=display)
    yield conn
    conn.disconnect()


def set_size_hints(conn, wid, min_size, max_size):
    conn.core.ChangeProperty(
        PropMode.Replace,
        wid,
        Atom.WM_NORMAL_HINTS,
        Atom.WM_SIZE_HINTS,
        32,
        18,
        struct.pack("18I", P_MIN_SIZE | P_MAX_SIZE, 0, 0, 0, 0, *min_size, *max_size, *[0] * 9),
    )


def create_window(conn, width, height, fixed_size):
    screen = conn.get_setup().roots[conn.pref_screen]
    wid = conn.generate_id()
    conn.core.CreateWindow(
        screen.root_depth,
        wid,
        screen.root,
        0,
        0,
        width,
        height,
        0,
        WindowClass.InputOutput,
        screen.root_visual,
        # Xwayland only commits a buffer, and so maps the window, once it has content
        CW.BackPixel | CW.EventMask,
        [screen.white_pixel, EventMask.StructureNotify],
    )
    conn.core.ChangeProperty(
        PropMode.Replace, wid, Atom.WM_NAME, Atom.STRING, 8, len(NAME), NAME.encode()
    )
    if fixed_size:
        # min = max makes Qtile float the window, as it does with plugin UIs
        set_size_hints(conn, wid, (width, height), (width, height))
    conn.core.MapWindow(wid)
    conn.flush()
    return wid


def request_size(conn, wid, width, height):
    conn.core.ConfigureWindow(wid, ConfigWindow.Width | ConfigWindow.Height, [width, height])
    conn.flush()


def x_size(conn, wid):
    geometry = conn.core.GetGeometry(wid).reply()
    return geometry.width, geometry.height


@Retry(ignore_exceptions=(AssertionError, KeyError))
def assert_size(wmanager, conn, wid, size):
    info = window_by_name(wmanager.c, NAME).info()
    assert (info["width"], info["height"]) == size
    assert x_size(conn, wid) == size


def wait_for_configure_notify(conn):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        event = conn.poll_for_event()
        if isinstance(event, xcffib.xproto.ConfigureNotifyEvent):
            return event
        if event is None:
            time.sleep(0.05)
    pytest.fail("No ConfigureNotify received")


def drain_events(conn):
    while conn.poll_for_event():
        pass


def test_floating_window_resizes_itself(wmanager, x11_conn):
    wid = create_window(x11_conn, 300, 200, fixed_size=True)
    assert_size(wmanager, x11_conn, wid, (300, 200))
    assert window_by_name(wmanager.c, NAME).info()["floating"]

    # The request is clamped to the size hints
    set_size_hints(x11_conn, wid, (100, 100), (500, 350))
    request_size(x11_conn, wid, 600, 400)
    assert_size(wmanager, x11_conn, wid, (500, 350))

    request_size(x11_conn, wid, 400, 250)
    assert_size(wmanager, x11_conn, wid, (400, 250))

    # Qtile keeps the new size when it places the window again
    wmanager.c.group["b"].toscreen()
    wmanager.c.group["a"].toscreen()
    assert_size(wmanager, x11_conn, wid, (400, 250))


def test_tiled_window_keeps_its_size(wmanager, x11_conn):
    wid = create_window(x11_conn, 300, 200, fixed_size=False)
    window = Retry(ignore_exceptions=(KeyError,))(window_by_name)(wmanager.c, NAME)
    info = window.info()
    assert not info["floating"]
    tiled_size = (info["width"], info["height"])
    assert_size(wmanager, x11_conn, wid, tiled_size)
    drain_events(x11_conn)

    # Refused, but the client still gets an answer
    request_size(x11_conn, wid, 123, 45)
    event = wait_for_configure_notify(x11_conn)
    assert (event.width, event.height) == tiled_size
    assert_size(wmanager, x11_conn, wid, tiled_size)
