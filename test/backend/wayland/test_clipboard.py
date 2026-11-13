import subprocess

from test.backend.wayland.conftest import (
    CLIPBOARD,
    CLIPBOARD_HELPER,
    list_types,
    make_test_env,
    qtile_eval,
    selection_types,
    settled_fd_count,
    wait_until,
)

MIME_TYPES = ["text/plain;charset=utf-8", "text/plain"]


def offer(wmanager, name, mime_types=MIME_TYPES, payload="payload"):
    wmanager.c.eval(f"{CLIPBOARD_HELPER}.offer(self.core, {name!r}, {mime_types!r}, {payload!r})")


def paste(wmanager, mime_type="text/plain"):
    return subprocess.run(
        ["wl-paste", "--no-newline", "--type", mime_type],
        env=make_test_env(wmanager),
        capture_output=True,
        timeout=10,
    )


def start_read(wmanager, name, mime_type="text/plain"):
    wmanager.c.eval(f"{CLIPBOARD_HELPER}.start_read(self.core, {name!r}, {mime_type!r})")


def read_result(wmanager, name):
    return qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.reads.get({name!r}, 'pending')")


def wait_for_read(wmanager, name):
    wait_until(lambda: read_result(wmanager, name) != "pending")
    return read_result(wmanager, name)


def test_source_is_listed_and_pasted(wmanager):
    offer(wmanager, "a")

    listed = list_types(wmanager)
    assert listed.returncode == 0
    assert set(listed.stdout.split()) == set(MIME_TYPES)

    pasted = paste(wmanager)
    assert pasted.returncode == 0
    assert pasted.stdout == b"payload"
    assert qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.events") == [("send", "a", "text/plain")]


def test_client_selection_is_read(wmanager, copy):
    copy("hello")

    assert qtile_eval(wmanager, f"{CLIPBOARD}.selection_owner(self.core)") is None
    assert "text/plain" in selection_types(wmanager)

    start_read(wmanager, "r")
    assert wait_for_read(wmanager, "r") == "hello"


def test_replacing_source_cancels_it_once(wmanager, copy):
    offer(wmanager, "a")
    copy("client")

    wait_until(lambda: ("cancelled", "a") in qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.events"))
    assert qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.events").count(("cancelled", "a")) == 1
    assert qtile_eval(wmanager, f"{CLIPBOARD}.selection_owner(self.core)") is None


def test_destroy_cancels_once_and_clears_selection(wmanager):
    offer(wmanager, "a")
    wmanager.c.eval(f"{CLIPBOARD_HELPER}.destroy('a')")

    assert qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.events").count(("cancelled", "a")) == 1
    assert qtile_eval(wmanager, f"{CLIPBOARD}.selection_mime_types(self.core)") == []
    assert list_types(wmanager).returncode != 0


def test_read_of_unoffered_mime_type_or_no_selection_is_none(wmanager):
    offer(wmanager, "a")
    start_read(wmanager, "unoffered", "image/png")
    assert read_result(wmanager, "unoffered") is None

    wmanager.c.eval(f"{CLIPBOARD_HELPER}.destroy('a')")
    start_read(wmanager, "empty")
    assert read_result(wmanager, "empty") is None


def test_listeners_run_after_the_change_not_inside_it(wmanager):
    wmanager.c.eval(f"{CLIPBOARD_HELPER}.add_recording_listener(self.core, 'L')")
    offer(wmanager, "x")

    wait_until(
        lambda: ("L", False, "x") in qtile_eval(wmanager, f"{CLIPBOARD_HELPER}.listener_calls")
    )


def test_no_fd_leak_across_pastes_and_reads(wmanager, copy):
    # Each phase is measured against its own owner: replacing the wl-copy selection ends
    # the wl-copy process, which closes its connection and so changes the count.
    copy("client")
    start_read(wmanager, "warm")
    assert wait_for_read(wmanager, "warm") == "client"
    baseline = settled_fd_count(wmanager)
    for i in range(10):
        start_read(wmanager, f"read{i}")
        assert wait_for_read(wmanager, f"read{i}") == "client"
    assert settled_fd_count(wmanager) == baseline

    offer(wmanager, "a")
    assert paste(wmanager).stdout == b"payload"
    baseline = settled_fd_count(wmanager)
    for _ in range(10):
        assert paste(wmanager).stdout == b"payload"
    assert settled_fd_count(wmanager) == baseline
