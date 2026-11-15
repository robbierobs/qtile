import asyncio
import threading
from types import SimpleNamespace

import pytest

import libqtile.bar
import libqtile.config
import libqtile.confreader
import libqtile.layout
import libqtile.widget
from libqtile.widget.helpers.status_notifier.statusnotifier import StatusNotifierItem
from test.helpers import Retry  # noqa: I001


@Retry(ignore_exceptions=(AssertionError,))
def wait_for_icon(widget, hidden=True, prop="width"):
    width = widget.info()[prop]
    if hidden:
        assert width == 0
    else:
        assert width > 0


@Retry(ignore_exceptions=(AssertionError,))
def check_fullscreen(windows, fullscreen=True):
    full = windows()[0]["fullscreen"]
    assert full is fullscreen


@pytest.fixture(scope="function")
def sni_config(request, manager_nospawn):
    """
    Fixture provides a manager instance with StatusNotifier in the bar.

    Widget can be customised via parameterize.
    """

    class SNIConfig(libqtile.confreader.Config):
        """Config for the test."""

        auto_fullscreen = True
        keys = []
        mouse = []
        groups = [
            libqtile.config.Group("a"),
        ]
        layouts = [libqtile.layout.Max()]
        floating_layout = libqtile.resources.default_config.floating_layout
        screens = [
            libqtile.config.Screen(
                top=libqtile.bar.Bar(
                    [libqtile.widget.StatusNotifier(**getattr(request, "param", dict()))],
                    50,
                ),
            )
        ]

    yield SNIConfig


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_defaults(manager_nospawn, sni_config):
    """Check that widget displays and removes icon."""
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    assert widget.info()["width"] == 0

    win = manager_nospawn.test_window("TestSNI", export_sni=True)
    wait_for_icon(widget, hidden=False)

    # Kill it and icon disappears
    manager_nospawn.kill_window(win)
    wait_for_icon(widget, hidden=True)


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_defaults_vertical_bar(manager_nospawn, sni_config):
    """Check that widget displays and removes icon."""
    screen = sni_config.screens[0]
    screen.left = screen.top
    screen.top = None
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    assert widget.info()["height"] == 0

    win = manager_nospawn.test_window("TestSNI", export_sni=True)
    wait_for_icon(widget, hidden=False, prop="height")

    # Kill it and icon disappears
    manager_nospawn.kill_window(win)
    wait_for_icon(widget, hidden=True, prop="height")


@pytest.mark.parametrize("sni_config", [{"icon_size": 35}], indirect=True)
@pytest.mark.usefixtures("dbus")
def test_statusnotifier_icon_size(manager_nospawn, sni_config):
    """Check that widget displays and removes icon."""
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    assert widget.info()["width"] == 0

    win = manager_nospawn.test_window("TestSNI", export_sni=True)
    wait_for_icon(widget, hidden=False)

    # Width should be icon_size (35) + 2 * padding (3) = 41
    assert widget.info()["width"] == 41

    manager_nospawn.kill_window(win)


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_left_click(manager_nospawn, sni_config):
    """Check `activate` method when left-clicking widget."""
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    windows = manager_nospawn.c.windows

    assert widget.info()["width"] == 0

    win = manager_nospawn.test_window("TestSNILeftClick", export_sni=True)
    wait_for_icon(widget, hidden=False)

    # Check we have window and that it's not fullscreen
    assert len(windows()) == 1
    check_fullscreen(windows, False)

    # Left click will toggle fullscreen
    manager_nospawn.c.bar["top"].fake_button_press(10, 0, 1)
    check_fullscreen(windows, True)

    # Left click again will restore window
    manager_nospawn.c.bar["top"].fake_button_press(10, 0, 1)
    check_fullscreen(windows, False)

    manager_nospawn.kill_window(win)
    assert not windows()


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_left_click_vertical_bar(manager_nospawn, sni_config):
    """Check `activate` method when left-clicking widget in vertical bar."""
    screen = sni_config.screens[0]
    screen.left = screen.top
    screen.top = None

    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    windows = manager_nospawn.c.windows

    assert widget.info()["height"] == 0

    win = manager_nospawn.test_window("TestSNILeftClick", export_sni=True)
    wait_for_icon(widget, hidden=False, prop="height")

    # Check we have window and that it's not fullscreen
    assert len(windows()) == 1
    check_fullscreen(windows, False)

    # Left click will toggle fullscreen
    manager_nospawn.c.bar["left"].fake_button_press(0, 10, 1)
    check_fullscreen(windows, True)

    # Left click again will restore window
    manager_nospawn.c.bar["left"].fake_button_press(0, 10, 1)
    check_fullscreen(windows, False)

    manager_nospawn.kill_window(win)
    assert not windows()


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_decodes_icons_off_event_loop(manager_nospawn, sni_config):
    """Icons are decoded in a worker thread, then drawn from the cache."""
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    widget.eval(
        "import threading\n"
        "self._decode_threads = []\n"
        # eval's locals aren't visible inside the function, so pass them in
        "def _record(item, size, orig=self._prepare_icon, threads=self._decode_threads,"
        " threading=threading):\n"
        "    threads.append(threading.current_thread() is threading.main_thread())\n"
        "    return orig(item, size)\n"
        "self._prepare_icon = _record"
    )

    manager_nospawn.test_window("TestSNI", export_sni=True)
    wait_for_icon(widget, hidden=False)

    @Retry(ignore_exceptions=(AssertionError,))
    def decoded():
        assert widget.eval("self._decode_threads") == "[False]"
        # The drawn icon is the cached one, already decoded at the size
        # draw_image paints it
        assert (
            widget.eval(
                "(lambda w: all(images is item.images"
                " and '_pattern' in icon._scaled[1].__dict__"
                " for item in w.available_icons"
                " for images, icon in [w._ready_icons[id(item)]]))(self)"
            )
            == "True"
        )

    decoded()


@pytest.mark.usefixtures("dbus")
def test_statusnotifier_icon_lookup_off_event_loop(manager_nospawn, sni_config, tmp_path):
    """Icon theme lookups run in a worker thread, and the icon still appears."""
    # The test window exports no icon name, so the item is given one from hicolor
    # and an empty icon theme directory: the custom lookup misses, the xdg one finds it
    theme = tmp_path / "theme"
    theme.mkdir()
    manager_nospawn.start(sni_config)
    widget = manager_nospawn.c.widget["statusnotifier"]
    widget.eval(
        "import threading\n"
        "from libqtile.widget.helpers.status_notifier import statusnotifier as sn\n"
        "self._loop_thread = threading.get_ident()\n"
        "self._lookup_threads = []\n"
        # eval's locals aren't visible inside the function, so pass them in
        "def _recorder(name, orig, threads=self._lookup_threads, threading=threading):\n"
        "    def record(item, *args):\n"
        "        threads.append((name, threading.get_ident()))\n"
        "        return orig(item, *args)\n"
        "    return record\n"
        "for name in ('_get_custom_icon', '_get_xdg_icon'):\n"
        "    setattr(sn.StatusNotifierItem, name,"
        " _recorder(name, getattr(sn.StatusNotifierItem, name)))\n"
        "async def _named_icon(self, fallback=True, orig=sn.StatusNotifierItem._get_local_icon):\n"
        "    async def get_icon_name():\n"
        "        return 'account-apple'\n"
        "    async def get_icon_theme_path():\n"
        f"        return {str(theme)!r}\n"
        "    self.item.get_icon_name = get_icon_name\n"
        "    self.item.get_icon_theme_path = get_icon_theme_path\n"
        "    return await orig(self, fallback=fallback)\n"
        "sn.StatusNotifierItem._get_local_icon = _named_icon"
    )

    manager_nospawn.test_window("TestSNI", export_sni=True)
    wait_for_icon(widget, hidden=False)

    assert widget.eval("sorted({n for n, _ in self._lookup_threads})") == (
        "['_get_custom_icon', '_get_xdg_icon']"
    )
    assert (
        widget.eval("(lambda w: [n for n, t in w._lookup_threads if t == w._loop_thread])(self)")
        == "[]"
    )


def test_statusnotifier_slow_lookup_does_not_overwrite_newer_icon():
    async def get_icon_theme_path():
        return None

    names = iter(["old", "new"])

    async def get_icon_name():
        return next(names)

    old_started = threading.Event()
    new_done = threading.Event()

    def lookup(name):
        if name == "old":
            old_started.set()
            new_done.wait(5)
        return f"icon-{name}"

    item = StatusNotifierItem(None, "service")
    item.item = SimpleNamespace(
        get_icon_name=get_icon_name, get_icon_theme_path=get_icon_theme_path
    )
    item._get_xdg_icon = lookup

    async def run():
        old = asyncio.create_task(item._get_local_icon(fallback=False))
        while not old_started.is_set():
            await asyncio.sleep(0)
        await item._get_local_icon(fallback=False)
        new_done.set()
        await old

    asyncio.run(run())

    assert item.icon == "icon-new"
