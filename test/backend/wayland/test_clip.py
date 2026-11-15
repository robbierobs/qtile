import ast
import shutil

import pytest
import xcffib

from test.backend.wayland.conftest import new_xdg_client
from test.backend.wayland.test_xwayland_configure import NAME as XWAYLAND_NAME
from test.backend.wayland.test_xwayland_configure import create_window
from test.conftest import dualmonitor
from test.helpers import Retry, window_by_name

pytestmark = dualmonitor

HOVERED = "self.hovered_window.name if self.hovered_window else None"
XDG_NAME = "clip"
PLAIN_NAME = "plain"
Y = 100
WIDTH = 300
HEIGHT = 200
MID_Y = Y + HEIGHT // 2


@pytest.fixture(params=["xdg", "xwayland"])
def kind(request):
    if request.param == "xwayland" and shutil.which("Xwayland") is None:
        pytest.skip("Xwayland is not installed")
    return request.param


@pytest.fixture
def x11_conn(wmanager, kind):
    if kind == "xdg":
        yield None
        return

    display = wmanager.backend.env["DISPLAY"]
    if not display:
        pytest.skip("Qtile was built without Xwayland")

    conn = xcffib.connect(display=display)
    yield conn
    conn.disconnect()


def output_rect(wmanager, index):
    return wmanager.c.screen[index].info()["rect"]


def edge(wmanager):
    """Right edge of the first output, which is where the test window straddles."""
    rect = output_rect(wmanager, 0)
    return rect["x"] + rect["width"]


def straddling_box(wmanager):
    """A window whose right half lies on the second output."""
    return (edge(wmanager) - WIDTH // 2, Y, WIDTH, HEIGHT)


def on_output1(wmanager):
    return edge(wmanager) - 100, MID_Y


def on_output2(wmanager):
    return edge(wmanager) + 100, MID_Y


def hovered_at(wmanager, x, y):
    """Name of the window the pointer hovers at (x, y), or None."""
    wmanager.c.eval("setattr(self, 'hovered_window', None)")
    wmanager.backend.fake_motion(x, y)
    wmanager.c.sync()
    return ast.literal_eval(wmanager.c.eval(f"repr({HOVERED})"))


def wait_for_window(wmanager, name):
    return Retry(ignore_exceptions=(KeyError,))(window_by_name)(wmanager.c, name)


def open_window(wmanager, kind, x11_conn):
    """Open a window of the given kind and return its name."""
    if kind == "xdg":
        new_xdg_client(wmanager, XDG_NAME)
        return XDG_NAME

    create_window(x11_conn, WIDTH, HEIGHT, fixed_size=False)
    wmanager.c.sync()
    return XWAYLAND_NAME


def place_window(wmanager, name, box, borderwidth=0):
    """Float the window and place it; returns its command object."""
    window = wait_for_window(wmanager, name)
    window.toggle_floating()
    x, y, width, height = box
    window.place(x, y, width, height, borderwidth, None)
    wmanager.c.sync()
    return window


def clip_to_output(window, wmanager, index):
    rect = output_rect(wmanager, index)
    window.clip_to(rect["x"], rect["y"], rect["width"], rect["height"])


def paint_buffer_border(wmanager, name, width, height, border):
    """Paint a QW_BORDER_BUFFER ring around the window; Python place() only makes rect borders."""
    wid = window_by_name(wmanager.c, name).info()["id"]
    wmanager.c.eval(
        f"""
import cairocffi
from libqtile.backend.wayland._ffi import ffi, lib
view = self.windows_map[{wid}]._ptr
self._buffer_surface = cairocffi.ImageSurface(
    cairocffi.FORMAT_ARGB32, {width + 2 * border}, {height + 2 * border}
)
borders = ffi.new("struct qw_border[1]")
borders[0].type = lib.QW_BORDER_BUFFER
borders[0].width = {border}
addr = ffi.cast("uintptr_t", self._buffer_surface._pointer)
borders[0].buffer.surface = ffi.cast("cairo_surface_t *", addr)
lib.qw_view_paint_borders(view, borders, 1)
"""
    )


def test_before_clip_hovers_on_both_outputs(wmanager, kind, x11_conn):
    """Before any clip_to, the pointer hovers the window on both outputs (baseline)."""
    name = open_window(wmanager, kind, x11_conn)
    place_window(wmanager, name, straddling_box(wmanager))

    assert hovered_at(wmanager, *on_output1(wmanager)) == name
    assert hovered_at(wmanager, *on_output2(wmanager)) == name


def test_clip_to_removes_hover_outside_rectangle(wmanager, kind, x11_conn):
    """C1: only the part inside the clip rectangle takes pointer input."""
    name = open_window(wmanager, kind, x11_conn)
    window = place_window(wmanager, name, straddling_box(wmanager))

    clip_to_output(window, wmanager, 0)

    assert hovered_at(wmanager, *on_output2(wmanager)) is None
    assert hovered_at(wmanager, *on_output1(wmanager)) == name


def test_place_removes_clip(wmanager, kind, x11_conn):
    """C2: the next place() drops the clip, whatever its arguments."""
    name = open_window(wmanager, kind, x11_conn)
    box = straddling_box(wmanager)
    window = place_window(wmanager, name, box)
    clip_to_output(window, wmanager, 0)
    assert hovered_at(wmanager, *on_output2(wmanager)) is None

    window.place(*box, 0, None)
    wmanager.c.sync()

    assert hovered_at(wmanager, *on_output2(wmanager)) == name


def test_clip_applies_to_borders(wmanager, kind, x11_conn):
    """C3: borders are clipped as well as the surfaces."""
    name = open_window(wmanager, kind, x11_conn)
    x, y, width, height = straddling_box(wmanager)
    border = 20
    window = place_window(wmanager, name, (x, y, width, height), borderwidth=border)
    # The window's outer box is the content plus a border on each side: the left border
    # is inside the clip, the right border lies beyond the first output's edge.
    left_border = (x + border // 2, MID_Y)
    right_border = (x + border + width + border // 2, MID_Y)

    assert hovered_at(wmanager, *right_border) == name

    clip_to_output(window, wmanager, 0)

    assert hovered_at(wmanager, *right_border) is None
    assert hovered_at(wmanager, *left_border) == name


def test_clip_applies_to_buffer_borders(wmanager, kind, x11_conn):
    """C3: QW_BORDER_BUFFER rings are clipped like rect borders."""
    name = open_window(wmanager, kind, x11_conn)
    x, y, width, height = straddling_box(wmanager)
    border = 20
    window = place_window(wmanager, name, (x, y, width, height))
    paint_buffer_border(wmanager, name, width, height, border)
    left_border = (x + border // 2, MID_Y)
    right_border = (x + border + width + border // 2, MID_Y)

    assert hovered_at(wmanager, *right_border) == name

    clip_to_output(window, wmanager, 0)

    assert hovered_at(wmanager, *right_border) is None
    assert hovered_at(wmanager, *left_border) == name


def test_clip_outside_window_keeps_it_visible(wmanager, kind, x11_conn):
    """C5: an empty intersection draws nothing but leaves the window visible."""
    name = open_window(wmanager, kind, x11_conn)
    window = place_window(wmanager, name, straddling_box(wmanager))

    window.clip_to(0, 0, 100, 100)

    assert hovered_at(wmanager, *on_output1(wmanager)) is None
    assert hovered_at(wmanager, *on_output2(wmanager)) is None
    assert window.is_visible()


def test_clip_on_hidden_window_then_kill(wmanager, kind, x11_conn, capfd):
    """C8: clip_to on a hidden window, then unhide and kill, leaves Qtile running with no error."""
    name = open_window(wmanager, kind, x11_conn)
    window = place_window(wmanager, name, straddling_box(wmanager))

    # Group "b" is shown on the second output, so switching to it would swap the groups
    # and leave "a" visible. Group "c" is on no output and hides "a".
    wmanager.c.group["c"].toscreen()
    assert not window.is_visible()
    clip_to_output(window, wmanager, 0)

    wmanager.c.group["a"].toscreen()
    assert window.is_visible()

    window.kill()
    wmanager.c.sync()

    assert wmanager.c.status() == "OK"
    captured = capfd.readouterr()
    assert "Traceback" not in captured.out + captured.err


def test_unclipped_window_is_unaffected_by_clipping_another(wmanager, kind, x11_conn):
    """C6: a window that never calls clip_to reports the same info and hover results."""
    new_xdg_client(wmanager, PLAIN_NAME)
    plain = place_window(wmanager, PLAIN_NAME, (100, 400, 200, 100))
    name = open_window(wmanager, kind, x11_conn)
    window = place_window(wmanager, name, straddling_box(wmanager))
    info_before = plain.info()
    inside_plain = (150, 450)
    assert hovered_at(wmanager, *inside_plain) == PLAIN_NAME

    clip_to_output(window, wmanager, 0)

    assert plain.info() == info_before
    assert hovered_at(wmanager, *inside_plain) == PLAIN_NAME
    assert hovered_at(wmanager, *on_output2(wmanager)) is None


@pytest.mark.parametrize("kind", ["xwayland"])
@pytest.mark.skipif(shutil.which("Xwayland") is None, reason="Xwayland is not installed")
def test_xwayland_surface_is_clipped_only_while_clip_to_is_set(wmanager, kind, x11_conn):
    """A fixed-size 400x300 XWayland surface in a 200x200 box draws past the box until clip_to."""
    create_window(x11_conn, 400, 300, fixed_size=True)
    wmanager.c.sync()
    window = wait_for_window(wmanager, XWAYLAND_NAME)
    window.place(100, 100, 200, 200, 0, None)
    wmanager.c.sync()
    beyond_box = (350, 150)

    assert hovered_at(wmanager, *beyond_box) == XWAYLAND_NAME

    clip_to_output(window, wmanager, 0)
    assert hovered_at(wmanager, *beyond_box) is None

    window.place(100, 100, 200, 200, 0, None)
    wmanager.c.sync()
    assert hovered_at(wmanager, *beyond_box) == XWAYLAND_NAME
