#ifndef QW_XWAYLAND_KEYBOARD_GRAB_H
#define QW_XWAYLAND_KEYBOARD_GRAB_H

#include <stdbool.h>
#include <wayland-server-core.h>
#include <wlr/types/wlr_compositor.h>

struct qw_server;

// Server side of xwayland-keyboard-grab-unstable-v1, which wlroots does not provide.
// Xwayland sends grab_keyboard when an X11 client takes an active keyboard grab (e.g. a
// remote desktop client in immersive mode) and destroys the grab when the X11 grab ends.
// This is the X11 counterpart of keyboard-shortcuts-inhibit-unstable-v1.
struct qw_xwayland_keyboard_grab_manager {
    struct qw_server *server;
    struct wl_global *global;
    struct wl_list grabs; // qw_xwayland_keyboard_grab.link
};

struct qw_xwayland_keyboard_grab {
    struct wl_resource *resource;
    struct wlr_surface *surface; // NULL once the surface has been destroyed
    struct wl_listener surface_destroy;
    struct wl_list link; // qw_xwayland_keyboard_grab_manager.grabs
};

struct qw_xwayland_keyboard_grab_manager *
qw_xwayland_keyboard_grab_manager_create(struct qw_server *server);

void qw_xwayland_keyboard_grab_manager_destroy(struct qw_xwayland_keyboard_grab_manager *manager);

// Whether Xwayland holds a keyboard grab on the given surface
bool qw_xwayland_keyboard_grab_manager_has_grab(struct qw_xwayland_keyboard_grab_manager *manager,
                                                struct wlr_surface *surface);

#endif /* QW_XWAYLAND_KEYBOARD_GRAB_H */
