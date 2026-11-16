import pytest

from test.backend.wayland.conftest import WaylandBackend
from test.helpers import BareConfig, TestManager

HOLDS = (
    "__import__('libqtile.backend.wayland._ffi', fromlist=['lib'])"
    ".lib.qw_server_holds_egl_context(self.core.qw)"
)


@pytest.fixture
def start(request, wayland_session):
    """Start a nested Qtile with extra environment on top of the test session's."""
    managers = []

    def _start(**extra):
        manager = TestManager(
            WaylandBackend({**wayland_session, **extra}), request.config.getoption("--debuglog")
        )
        manager.__enter__()
        managers.append(manager)
        try:
            manager.start(BareConfig)
        except AssertionError:
            if extra.get("WLR_RENDERER") == "gles2":
                pytest.skip("no GLES2 renderer on this machine")
            raise
        return manager

    yield _start
    for manager in managers:
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "renderer,hold,held",
    [
        ("gles2", "1", "True"),
        ("gles2", "0", "False"),
        ("gles2", None, "False"),
        ("pixman", "1", "False"),
    ],
)
def test_hold_egl_context(start, renderer, hold, held):
    env = {"WLR_RENDERER": renderer}
    if hold is not None:
        env["QTILE_WAYLAND_HOLD_EGL_CONTEXT"] = hold
    manager = start(**env)

    # A mapped window means wlroots has uploaded a texture and rendered a frame
    manager.test_window("one")
    manager.c.sync()

    assert manager.c.eval(HOLDS) == held
    assert manager.c.window.info()["name"] == "one"
