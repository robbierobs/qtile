from unittest.mock import Mock

import pytest

from libqtile import config, widget
from libqtile.bar import Bar
from libqtile.widget.groupbox import GroupBox
from test.helpers import BareConfig, Retry


class GroupBoxConfig(BareConfig):
    screens = [
        config.Screen(
            top=Bar([widget.GroupBox(), widget.GroupBox(name="has_markup", markup=True)], 24)
        )
    ]
    groups = [config.Group("1", label="<sup>1</sup>")]


class GroupSwitchConfig(BareConfig):
    screens = [config.Screen(top=Bar([widget.GroupBox()], 24))]
    groups = [config.Group("1"), config.Group("2")]


groupbox_config = pytest.mark.parametrize("manager", [GroupBoxConfig], indirect=True)


@groupbox_config
def test_groupbox_markup(manager):
    """Group labels can support markup but this is disabled by default."""
    no_markup = manager.c.widget["groupbox"]
    has_markup = manager.c.widget["has_markup"]

    # If markup is disabled, text will include markup tags so widget will be wider
    assert no_markup.info()["width"] > has_markup.info()["width"]


class _FakeQtile:
    def __init__(self):
        self.pending = []

    def call_soon(self, func, *args):
        self.pending.append(func)

    def run_iteration(self):
        pending, self.pending = self.pending, []
        for func in pending:
            func()


def _hooked_groupbox(length, drawn_length, bar_draw_queued=False):
    widget = GroupBox()
    widget.bar = Mock(_draw_queued=bar_draw_queued)
    widget.qtile = _FakeQtile()
    widget.calculate_length = Mock(return_value=length)
    widget.draw = Mock()
    widget._drawn_length = drawn_length
    return widget


def test_hook_unchanged_length_redraws_widget_only():
    widget = _hooked_groupbox(length=50, drawn_length=50)

    widget._hook_response()
    widget.qtile.run_iteration()

    assert widget.bar.draw.call_count == 0
    assert widget.draw.call_count == 1


def test_hook_changed_length_redraws_bar():
    widget = _hooked_groupbox(length=60, drawn_length=50)

    widget._hook_response()
    widget.qtile.run_iteration()

    assert widget.bar.draw.call_count == 1
    assert widget.draw.call_count == 0


def test_seven_hooks_in_one_iteration_draw_widget_once():
    widget = _hooked_groupbox(length=50, drawn_length=50)

    for _ in range(7):
        widget._hook_response()
    widget.qtile.run_iteration()

    assert widget.bar.draw.call_count == 0
    assert widget.draw.call_count == 1


def test_seven_hooks_in_one_iteration_calculate_length_once():
    widget = _hooked_groupbox(length=50, drawn_length=50)
    widget.draw = Mock()

    for _ in range(7):
        widget._hook_response()
    widget.qtile.run_iteration()

    assert widget.calculate_length.call_count == 1


def test_pending_draw_skipped_when_bar_draw_queued_meanwhile():
    widget = _hooked_groupbox(length=50, drawn_length=50)

    widget._hook_response()
    widget.bar._draw_queued = True
    widget.qtile.run_iteration()

    assert widget.draw.call_count == 0
    assert widget.bar.draw.call_count == 0


def test_hook_while_bar_draw_queued_adds_no_widget_draw():
    widget = _hooked_groupbox(length=50, drawn_length=50, bar_draw_queued=True)

    widget._hook_response()

    assert widget.qtile.pending == []
    assert widget.bar.draw.call_count == 0


@pytest.mark.parametrize("manager", [GroupSwitchConfig], indirect=True)
def test_groupbox_redraws_new_current_group(manager):
    widget = manager.c.widget["groupbox"]
    # Draw log, not pixels: the widget-only path must run after a switch that leaves its length alone
    widget.eval(
        "self._drawn_groups = []\n"
        "def _log(orig=self.draw, bar=self.bar, log=self._drawn_groups):\n"
        "    orig()\n"
        "    log.append(bar.screen.group.name)\n"
        "self.draw = _log"
    )

    manager.c.group["2"].toscreen()

    @Retry(ignore_exceptions=(AssertionError,))
    def redrawn_for_new_group():
        assert widget.eval("(self._drawn_groups or [None])[-1] == '2'") == "True"

    redrawn_for_new_group()
