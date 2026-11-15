import ast

import pytest

import libqtile.bar
import libqtile.config
import libqtile.widget
from libqtile import hook
from test.backend.wayland.conftest import new_xdg_client
from test.backend.wayland.test_clip import HOVERED, place_window
from test.conftest import BareConfig


class HoverCounter(libqtile.widget.TextBox):
    """TextBox that counts mouse_enter / mouse_leave / button presses."""

    def __init__(self, **kwargs):
        libqtile.widget.TextBox.__init__(self, **kwargs)
        self.enter_count = 0
        self.leave_count = 0
        self.press_count = 0

    def mouse_enter(self, x, y):
        self.enter_count += 1

    def mouse_leave(self, x, y):
        self.leave_count += 1

    def button_press(self, x, y, button):
        self.press_count += 1
        libqtile.widget.TextBox.button_press(self, x, y, button)


class HoverConfig(BareConfig):
    screens = [
        libqtile.config.Screen(
            top=libqtile.bar.Bar(
                [HoverCounter(text="hov", name="hov")],
                30,
            ),
        ),
    ]


hover_config = pytest.mark.parametrize("wmanager", [HoverConfig], indirect=True)


@hover_config
def test_bar_widget_pointer_events(wmanager):
    """Cursor crossing a bar widget fires mouse_enter / mouse_leave."""
    enter = 'self.widgets_map["hov"].enter_count'
    leave = 'self.widgets_map["hov"].leave_count'

    # Cursor outside the bar — no events.
    wmanager.backend.fake_motion(400, 400)
    wmanager.c.sync()
    assert int(wmanager.c.eval(enter)) == 0
    assert int(wmanager.c.eval(leave)) == 0

    # Move into the top bar over the widget.
    wmanager.backend.fake_motion(10, 10)
    wmanager.c.sync()
    assert int(wmanager.c.eval(enter)) == 1
    assert int(wmanager.c.eval(leave)) == 0

    # Move back out.
    wmanager.backend.fake_motion(400, 400)
    wmanager.c.sync()
    assert int(wmanager.c.eval(enter)) == 1
    assert int(wmanager.c.eval(leave)) == 1

    # Re-entering increments enter_count again.
    wmanager.backend.fake_motion(10, 10)
    wmanager.c.sync()
    assert int(wmanager.c.eval(enter)) == 2
    assert int(wmanager.c.eval(leave)) == 1


def test_hovered_window_clears_over_no_window(manager_nospawn):
    """Pointer over no window clears hovered_window; coming back re-fires client_mouse_enter."""
    name = "hover"

    class EnterCounter:
        def __init__(self):
            self.count = 0

        def __call__(self, client):
            self.count += 1

    class HoverEnterConfig(HoverConfig):
        enter = EnterCounter()
        hook.subscribe.client_mouse_enter(enter)

    manager_nospawn.start(HoverEnterConfig)
    try:
        new_xdg_client(manager_nospawn, name)
        rect = manager_nospawn.c.screen[0].info()["rect"]
        place_window(manager_nospawn, name, (rect["x"] + 100, rect["y"] + 100, 300, 200))
        inside = (rect["x"] + 200, rect["y"] + 200)
        empty = (rect["x"] + rect["width"] - 50, rect["y"] + rect["height"] - 50)

        def hovered():
            manager_nospawn.c.sync()
            return ast.literal_eval(manager_nospawn.c.eval(f"repr({HOVERED})"))

        def enters():
            return int(manager_nospawn.c.eval("self.config.enter.count"))

        manager_nospawn.backend.fake_motion(*inside)
        assert hovered() == name
        assert enters() == 1

        manager_nospawn.backend.fake_motion(*empty)
        assert hovered() is None

        manager_nospawn.backend.fake_motion(*inside)
        assert hovered() == name
        assert enters() == 2

        # A click on the bar, after the pointer went over no window, still reaches the bar.
        manager_nospawn.backend.fake_motion(*empty)
        assert hovered() is None
        manager_nospawn.backend.fake_click(10, 10)
        manager_nospawn.c.sync()
        assert int(manager_nospawn.c.eval('self.widgets_map["hov"].press_count')) == 1
    finally:
        hook.clear()
