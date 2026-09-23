#include "xwayland-keyboard-grab.h"
#include "server.h"
#include "util.h"
#include "xwayland-keyboard-grab-unstable-v1-protocol.h"

#include <stdlib.h>
#include <wlr/util/log.h>
#include <wlr/xwayland/server.h>

static void qw_xwayland_keyboard_grab_handle_destroy(struct wl_client *client,
                                                     struct wl_resource *resource) {
    UNUSED(client);
    wl_resource_destroy(resource);
}

static const struct zwp_xwayland_keyboard_grab_v1_interface grab_impl = {
    .destroy = qw_xwayland_keyboard_grab_handle_destroy,
};

static void qw_xwayland_keyboard_grab_handle_resource_destroy(struct wl_resource *resource) {
    struct qw_xwayland_keyboard_grab *grab = wl_resource_get_user_data(resource);
    wl_list_remove(&grab->surface_destroy.link);
    wl_list_remove(&grab->link);
    free(grab);
}

// The resource outlives its surface until Xwayland destroys it, so just make it inert.
static void qw_xwayland_keyboard_grab_handle_surface_destroy(struct wl_listener *listener,
                                                             void *data) {
    UNUSED(data);
    struct qw_xwayland_keyboard_grab *grab = wl_container_of(listener, grab, surface_destroy);
    wl_list_remove(&grab->surface_destroy.link);
    wl_list_init(&grab->surface_destroy.link);
    wl_list_remove(&grab->link);
    wl_list_init(&grab->link);
    grab->surface = NULL;
}

static void qw_xwayland_keyboard_grab_manager_handle_destroy(struct wl_client *client,
                                                             struct wl_resource *resource) {
    UNUSED(client);
    wl_resource_destroy(resource);
}

static void qw_xwayland_keyboard_grab_manager_handle_grab_keyboard(
    struct wl_client *client, struct wl_resource *resource, uint32_t id,
    struct wl_resource *surface_resource, struct wl_resource *seat_resource) {
    // Qtile only has one seat
    UNUSED(seat_resource);
    struct qw_xwayland_keyboard_grab_manager *manager = wl_resource_get_user_data(resource);
    struct wlr_surface *surface = wlr_surface_from_resource(surface_resource);

    struct qw_xwayland_keyboard_grab *grab = calloc(1, sizeof(*grab));
    if (grab == NULL) {
        wl_client_post_no_memory(client);
        return;
    }

    grab->resource = wl_resource_create(client, &zwp_xwayland_keyboard_grab_v1_interface,
                                        wl_resource_get_version(resource), id);
    if (grab->resource == NULL) {
        free(grab);
        wl_client_post_no_memory(client);
        return;
    }
    wl_resource_set_implementation(grab->resource, &grab_impl, grab,
                                   qw_xwayland_keyboard_grab_handle_resource_destroy);

    grab->surface = surface;
    grab->surface_destroy.notify = qw_xwayland_keyboard_grab_handle_surface_destroy;
    wl_signal_add(&surface->events.destroy, &grab->surface_destroy);
    wl_list_insert(&manager->grabs, &grab->link);

    wlr_log(WLR_DEBUG, "Xwayland grabbed the keyboard for surface %p", (void *)surface);
}

static const struct zwp_xwayland_keyboard_grab_manager_v1_interface manager_impl = {
    .destroy = qw_xwayland_keyboard_grab_manager_handle_destroy,
    .grab_keyboard = qw_xwayland_keyboard_grab_manager_handle_grab_keyboard,
};

static void qw_xwayland_keyboard_grab_manager_bind(struct wl_client *client, void *data,
                                                   uint32_t version, uint32_t id) {
    struct qw_xwayland_keyboard_grab_manager *manager = data;

    // Only Xwayland may use this protocol; any other client could use it to steal the
    // keyboard from Qtile's keybindings.
    struct wlr_xwayland_server *xwayland_server = manager->server->xwayland->server;
    if (xwayland_server == NULL || client != xwayland_server->client) {
        wl_client_post_implementation_error(client, "permission denied");
        return;
    }

    struct wl_resource *resource =
        wl_resource_create(client, &zwp_xwayland_keyboard_grab_manager_v1_interface, version, id);
    if (resource == NULL) {
        wl_client_post_no_memory(client);
        return;
    }
    wl_resource_set_implementation(resource, &manager_impl, manager, NULL);
}

struct qw_xwayland_keyboard_grab_manager *
qw_xwayland_keyboard_grab_manager_create(struct qw_server *server) {
    struct qw_xwayland_keyboard_grab_manager *manager = calloc(1, sizeof(*manager));
    if (manager == NULL) {
        return NULL;
    }

    manager->global =
        wl_global_create(server->display, &zwp_xwayland_keyboard_grab_manager_v1_interface, 1,
                         manager, qw_xwayland_keyboard_grab_manager_bind);
    if (manager->global == NULL) {
        free(manager);
        return NULL;
    }

    manager->server = server;
    wl_list_init(&manager->grabs);
    return manager;
}

// Must be called after Xwayland, the only client that can bind the manager, is destroyed.
void qw_xwayland_keyboard_grab_manager_destroy(struct qw_xwayland_keyboard_grab_manager *manager) {
    wl_global_destroy(manager->global);

    struct qw_xwayland_keyboard_grab *grab, *tmp;
    wl_list_for_each_safe(grab, tmp, &manager->grabs, link) {
        wl_list_remove(&grab->link);
        wl_list_init(&grab->link);
    }

    free(manager);
}

bool qw_xwayland_keyboard_grab_manager_has_grab(struct qw_xwayland_keyboard_grab_manager *manager,
                                                struct wlr_surface *surface) {
    if (surface == NULL) {
        return false;
    }

    struct qw_xwayland_keyboard_grab *grab;
    wl_list_for_each(grab, &manager->grabs, link) {
        if (grab->surface == surface) {
            return true;
        }
    }

    return false;
}
