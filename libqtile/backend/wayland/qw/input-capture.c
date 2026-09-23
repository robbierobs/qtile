#include "input-capture.h"
#include "cursor.h"
#include "keyboard.h"
#include "output.h"
#include "server.h"
#include "util.h"

#include <libeis.h>
#include <stdlib.h>
#include <string.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

static void qw_input_capture_log(struct eis *eis, enum eis_log_priority priority,
                                 const char *message, struct eis_log_context *ctx) {
    UNUSED(eis);
    UNUSED(ctx);
    enum wlr_log_importance importance = WLR_DEBUG;
    if (priority >= EIS_LOG_PRIORITY_ERROR) {
        importance = WLR_ERROR;
    } else if (priority >= EIS_LOG_PRIORITY_INFO) {
        importance = WLR_INFO;
    }
    wlr_log(importance, "[eis] %s", message);
}

static void qw_input_capture_remove_device(struct eis_device **device) {
    if (*device == NULL) {
        return;
    }
    eis_device_remove(*device);
    eis_device_unref(*device);
    *device = NULL;
}

// The output's zone, or false if it has none. EIS regions are unsigned, so outputs at
// negative layout coordinates are left out of both.
static bool qw_input_capture_output_zone(struct qw_server *server, struct qw_output *output,
                                         struct wlr_box *box) {
    wlr_output_layout_get_box(server->output_layout, output->wlr_output, box);
    return !wlr_box_empty(box) && box->x >= 0 && box->y >= 0;
}

size_t qw_input_capture_get_zones(struct qw_server *server, struct wlr_box *zones, size_t max) {
    size_t count = 0;
    struct qw_output *output;
    wl_list_for_each(output, &server->outputs, link) {
        struct wlr_box box;
        if (count < max && qw_input_capture_output_zone(server, output, &box)) {
            zones[count++] = box;
        }
    }
    return count;
}

static void qw_input_capture_add_output_regions(struct qw_input_capture *capture,
                                                struct eis_device *device) {
    struct qw_output *output;
    wl_list_for_each(output, &capture->server->outputs, link) {
        struct wlr_box box;
        // Deskflow builds its screen shape from the regions
        if (!qw_input_capture_output_zone(capture->server, output, &box)) {
            continue;
        }
        struct eis_region *region = eis_device_new_region(device);
        eis_region_set_offset(region, box.x, box.y);
        eis_region_set_size(region, box.width, box.height);
        eis_region_set_physical_scale(region, output->wlr_output->scale);
        eis_region_add(region);
        eis_region_unref(region);
    }
}

static void qw_input_capture_create_pointer(struct qw_input_capture *capture) {
    struct eis_device *pointer = eis_seat_new_device(capture->seat);
    eis_device_configure_name(pointer, "qtile captured pointer");
    eis_device_configure_type(pointer, EIS_DEVICE_TYPE_VIRTUAL);
    eis_device_configure_capability(pointer, EIS_DEVICE_CAP_POINTER);
    eis_device_configure_capability(pointer, EIS_DEVICE_CAP_BUTTON);
    eis_device_configure_capability(pointer, EIS_DEVICE_CAP_SCROLL);
    qw_input_capture_add_output_regions(capture, pointer);
    eis_device_add(pointer);
    eis_device_resume(pointer);
    capture->pointer = pointer;

    if (capture->active) {
        eis_device_start_emulating(pointer, capture->activation_id);
    }
}

static void qw_input_capture_create_keyboard(struct qw_input_capture *capture) {
    struct wlr_keyboard *wlr_keyboard = wlr_seat_get_keyboard(capture->server->seat);
    if (wlr_keyboard == NULL || wlr_keyboard->keymap_fd < 0 || wlr_keyboard->keymap_size == 0) {
        // Created once a keyboard with a keymap shows up
        return;
    }

    struct eis_device *keyboard = eis_seat_new_device(capture->seat);
    eis_device_configure_name(keyboard, "qtile captured keyboard");
    eis_device_configure_type(keyboard, EIS_DEVICE_TYPE_VIRTUAL);
    eis_device_configure_capability(keyboard, EIS_DEVICE_CAP_KEYBOARD);
    struct eis_keymap *keymap = eis_device_new_keymap(
        keyboard, EIS_KEYMAP_TYPE_XKB, wlr_keyboard->keymap_fd, wlr_keyboard->keymap_size);
    eis_keymap_add(keymap);
    eis_keymap_unref(keymap);
    eis_device_add(keyboard);
    eis_device_resume(keyboard);
    capture->keyboard = keyboard;

    if (capture->active) {
        eis_device_start_emulating(keyboard, capture->activation_id);
    }
}

static void qw_input_capture_handle_client_connect(struct qw_input_capture *capture,
                                                   struct eis_client *client) {
    // InputCapture clients receive input; anything else, or a second client, is refused
    if (eis_client_is_sender(client) || capture->client != NULL) {
        wlr_log(WLR_ERROR, "Refusing EIS client for input capture");
        eis_client_disconnect(client);
        return;
    }

    eis_client_connect(client);
    capture->client = client;

    capture->seat = eis_client_new_seat(client, "default");
    eis_seat_configure_capability(capture->seat, EIS_DEVICE_CAP_POINTER);
    eis_seat_configure_capability(capture->seat, EIS_DEVICE_CAP_BUTTON);
    eis_seat_configure_capability(capture->seat, EIS_DEVICE_CAP_SCROLL);
    eis_seat_configure_capability(capture->seat, EIS_DEVICE_CAP_KEYBOARD);
    eis_seat_add(capture->seat);
}

static void qw_input_capture_handle_seat_bind(struct qw_input_capture *capture,
                                              struct eis_event *event) {
    bool want_pointer = eis_event_seat_has_capability(event, EIS_DEVICE_CAP_POINTER) &&
                        eis_event_seat_has_capability(event, EIS_DEVICE_CAP_BUTTON) &&
                        eis_event_seat_has_capability(event, EIS_DEVICE_CAP_SCROLL);
    bool want_keyboard = eis_event_seat_has_capability(event, EIS_DEVICE_CAP_KEYBOARD);

    if (want_pointer && capture->pointer == NULL) {
        qw_input_capture_create_pointer(capture);
    } else if (!want_pointer) {
        qw_input_capture_remove_device(&capture->pointer);
    }

    capture->keyboard_bound = want_keyboard;
    if (want_keyboard && capture->keyboard == NULL) {
        qw_input_capture_create_keyboard(capture);
    } else if (!want_keyboard) {
        qw_input_capture_remove_device(&capture->keyboard);
    }
}

static void qw_input_capture_forget_client(struct qw_input_capture *capture) {
    qw_input_capture_remove_device(&capture->pointer);
    qw_input_capture_remove_device(&capture->keyboard);
    if (capture->seat != NULL) {
        eis_seat_unref(capture->seat);
        capture->seat = NULL;
    }
    capture->client = NULL;
    capture->keyboard_bound = false;
}

static void qw_input_capture_deactivate(struct qw_input_capture *capture, bool notify,
                                        bool has_position, double x, double y);
static void qw_input_capture_disable_notify(struct qw_input_capture *capture);

static int qw_input_capture_handle_eis_readable(int fd, uint32_t mask, void *data) {
    UNUSED(fd);
    UNUSED(mask);
    struct qw_input_capture *capture = data;

    eis_dispatch(capture->eis);

    struct eis_event *event;
    while ((event = eis_get_event(capture->eis)) != NULL) {
        switch (eis_event_get_type(event)) {
        case EIS_EVENT_CLIENT_CONNECT:
            qw_input_capture_handle_client_connect(capture, eis_event_get_client(event));
            break;
        case EIS_EVENT_CLIENT_DISCONNECT:
            if (eis_event_get_client(event) == capture->client) {
                qw_input_capture_deactivate(capture, true, false, 0, 0);
                qw_input_capture_disable_notify(capture);
                qw_input_capture_forget_client(capture);
            }
            eis_client_disconnect(eis_event_get_client(event));
            break;
        case EIS_EVENT_SEAT_BIND:
            if (eis_event_get_seat(event) == capture->seat) {
                qw_input_capture_handle_seat_bind(capture, event);
            }
            break;
        case EIS_EVENT_DEVICE_CLOSED:
            if (eis_event_get_device(event) == capture->pointer) {
                qw_input_capture_remove_device(&capture->pointer);
            } else if (eis_event_get_device(event) == capture->keyboard) {
                qw_input_capture_remove_device(&capture->keyboard);
            }
            break;
        default:
            // Receivers do not send input events; nothing else needs handling
            break;
        }
        eis_event_unref(event);
    }

    return 0;
}

struct qw_input_capture *qw_input_capture_create(struct qw_server *server, void *userdata) {
    struct qw_input_capture *capture = calloc(1, sizeof(*capture));
    if (capture == NULL) {
        wlr_log(WLR_ERROR, "failed to allocate input capture");
        return NULL;
    }

    capture->eis = eis_new(capture);
    if (capture->eis == NULL) {
        wlr_log(WLR_ERROR, "failed to create EIS context");
        free(capture);
        return NULL;
    }
    eis_log_set_handler(capture->eis, qw_input_capture_log);

    int ret = eis_setup_backend_fd(capture->eis);
    if (ret != 0) {
        wlr_log(WLR_ERROR, "failed to set up EIS backend: %d", ret);
        eis_unref(capture->eis);
        free(capture);
        return NULL;
    }

    capture->eis_source =
        wl_event_loop_add_fd(server->event_loop, eis_get_fd(capture->eis), WL_EVENT_READABLE,
                             qw_input_capture_handle_eis_readable, capture);
    if (capture->eis_source == NULL) {
        wlr_log(WLR_ERROR, "failed to watch EIS fd");
        eis_unref(capture->eis);
        free(capture);
        return NULL;
    }

    capture->server = server;
    capture->userdata = userdata;
    wl_list_insert(&server->input_captures, &capture->link);
    return capture;
}

void qw_input_capture_destroy(struct qw_input_capture *capture) {
    qw_input_capture_deactivate(capture, false, false, 0, 0);
    wl_list_remove(&capture->link);
    qw_input_capture_forget_client(capture);
    wl_event_source_remove(capture->eis_source);
    eis_unref(capture->eis);
    free(capture->barriers);
    free(capture);
}

int qw_input_capture_connect_eis(struct qw_input_capture *capture) {
    // The EIS connection lasts for the whole portal session
    if (capture->eis_connected) {
        wlr_log(WLR_ERROR, "input capture session is already connected to EIS");
        return -1;
    }

    int fd = eis_backend_fd_add_client(capture->eis);
    if (fd < 0) {
        wlr_log(WLR_ERROR, "failed to create EIS client connection: %d", fd);
        return -1;
    }
    capture->eis_connected = true;
    return fd;
}

void qw_input_capture_clear_barriers(struct qw_input_capture *capture) {
    free(capture->barriers);
    capture->barriers = NULL;
    capture->barrier_count = 0;
}

bool qw_input_capture_add_barrier(struct qw_input_capture *capture, uint32_t id, int x1, int y1,
                                  int x2, int y2) {
    if (x1 != x2 && y1 != y2) {
        return false;
    }

    struct qw_input_capture_barrier *barriers =
        realloc(capture->barriers, (capture->barrier_count + 1) * sizeof(*barriers));
    if (barriers == NULL) {
        return false;
    }
    capture->barriers = barriers;

    barriers[capture->barrier_count++] = (struct qw_input_capture_barrier){
        .id = id,
        .x1 = x1 < x2 ? x1 : x2,
        .y1 = y1 < y2 ? y1 : y2,
        .x2 = x1 < x2 ? x2 : x1,
        .y2 = y1 < y2 ? y2 : y1,
    };
    return true;
}

void qw_input_capture_enable(struct qw_input_capture *capture) { capture->enabled = true; }

// Stops forwarding and hands input back to Qtile
static void qw_input_capture_deactivate(struct qw_input_capture *capture, bool notify,
                                        bool has_position, double x, double y) {
    if (!capture->active) {
        return;
    }

    // Nothing may stay pressed on the receiver's side
    if (capture->keyboard != NULL) {
        for (size_t i = 0; i < capture->pressed_key_count; i++) {
            eis_device_keyboard_key(capture->keyboard, capture->pressed_keys[i], false);
        }
        eis_device_frame(capture->keyboard, eis_now(capture->eis));
        eis_device_stop_emulating(capture->keyboard);
    }
    if (capture->pointer != NULL) {
        for (size_t i = 0; i < capture->pressed_button_count; i++) {
            eis_device_button_button(capture->pointer, capture->pressed_buttons[i], false);
        }
        eis_device_frame(capture->pointer, eis_now(capture->eis));
        eis_device_stop_emulating(capture->pointer);
    }
    capture->pressed_key_count = 0;
    capture->pressed_button_count = 0;

    capture->active = false;
    struct qw_server *server = capture->server;
    server->active_input_capture = NULL;

    if (has_position) {
        wlr_cursor_warp_closest(server->cursor->cursor, NULL, x, y);
    }
    // Refocusing sets the cursor image again, which was unset on activation
    qw_cursor_update_pointer_focus(server->cursor);
    server->focus_current_window_cb(server->cb_data);

    if (notify && server->input_capture_deactivated_cb != NULL) {
        server->input_capture_deactivated_cb(capture->userdata, capture->activation_id);
    }
}

static void qw_input_capture_disable_notify(struct qw_input_capture *capture) {
    if (!capture->enabled) {
        return;
    }
    capture->enabled = false;
    if (capture->server->input_capture_disabled_cb != NULL) {
        capture->server->input_capture_disabled_cb(capture->userdata);
    }
}

void qw_input_capture_disable(struct qw_input_capture *capture) {
    // A portal-initiated disable emits neither Deactivated nor Disabled
    qw_input_capture_deactivate(capture, false, false, 0, 0);
    capture->enabled = false;
}

void qw_input_capture_release(struct qw_input_capture *capture, uint32_t activation_id,
                              bool has_position, double x, double y) {
    // Releases for an earlier activation are ignored
    if (!capture->active || activation_id != capture->activation_id) {
        wlr_log(WLR_DEBUG, "Ignoring input capture release for activation %u", activation_id);
        return;
    }
    qw_input_capture_deactivate(capture, false, has_position, x, y);
}

void qw_server_set_input_capture_release_key(struct qw_server *server, uint32_t keysym,
                                             uint32_t modifiers) {
    server->input_capture_release_keysym = keysym;
    server->input_capture_release_modifiers = modifiers;
}

static void qw_input_capture_activate(struct qw_input_capture *capture,
                                      const struct qw_input_capture_barrier *barrier, double x,
                                      double y) {
    struct qw_server *server = capture->server;

    capture->active = true;
    // The EIS emulation sequence must match the portal's activation id
    capture->activation_id++;
    server->active_input_capture = capture;

    eis_device_start_emulating(capture->pointer, capture->activation_id);
    if (capture->keyboard != NULL) {
        eis_device_start_emulating(capture->keyboard, capture->activation_id);
    }

    // Clients see the pointer and keyboard leave, so nothing stays pressed for them
    wlr_seat_pointer_notify_clear_focus(server->seat);
    wlr_seat_keyboard_notify_clear_focus(server->seat);
    wlr_cursor_unset_image(server->cursor->cursor);

    if (server->input_capture_activated_cb != NULL) {
        server->input_capture_activated_cb(capture->userdata, capture->activation_id, barrier->id,
                                           x, y);
    }
}

// Whether moving from (x, y) to (nx, ny) crosses the barrier
static bool qw_input_capture_barrier_crossed(const struct qw_input_capture_barrier *barrier,
                                             double x, double y, double nx, double ny) {
    // Barriers sit on the top or left edge of their pixels. The cursor is clamped to just
    // inside the layout, so it never reaches a right or bottom barrier without crossing.
    if (barrier->x1 == barrier->x2) {
        double bx = barrier->x1;
        if (!((x < bx && nx >= bx) || (x >= bx && nx < bx))) {
            return false;
        }
        double yc = y + (ny - y) * (bx - x) / (nx - x);
        return yc >= barrier->y1 && yc < barrier->y2 + 1;
    }

    double by = barrier->y1;
    if (!((y < by && ny >= by) || (y >= by && ny < by))) {
        return false;
    }
    double xc = x + (nx - x) * (by - y) / (ny - y);
    return xc >= barrier->x1 && xc < barrier->x2 + 1;
}

bool qw_input_capture_check_barriers(struct qw_server *server, double x, double y, double dx,
                                     double dy) {
    if (server->active_input_capture != NULL || (dx == 0 && dy == 0)) {
        return false;
    }

    struct qw_input_capture *capture;
    wl_list_for_each(capture, &server->input_captures, link) {
        // Without a pointer device the receiver cannot follow the capture
        if (!capture->enabled || capture->pointer == NULL) {
            continue;
        }
        for (size_t i = 0; i < capture->barrier_count; i++) {
            const struct qw_input_capture_barrier *barrier = &capture->barriers[i];
            if (qw_input_capture_barrier_crossed(barrier, x, y, x + dx, y + dy)) {
                qw_input_capture_activate(capture, barrier, x + dx, y + dy);
                return true;
            }
        }
    }

    return false;
}

bool qw_input_capture_handle_motion(struct qw_server *server, double dx, double dy) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }
    // The client may close its pointer mid-capture; input is still captured
    if (capture->pointer != NULL) {
        eis_device_pointer_motion(capture->pointer, dx, dy);
    }
    return true;
}

static bool qw_input_capture_track(uint32_t *pressed, size_t *count, size_t max, uint32_t code,
                                   bool press) {
    for (size_t i = 0; i < *count; i++) {
        if (pressed[i] == code) {
            if (!press) {
                pressed[i] = pressed[--*count];
            }
            return true;
        }
    }
    if (press && *count < max) {
        pressed[(*count)++] = code;
        return true;
    }
    return false;
}

bool qw_input_capture_handle_button(struct qw_server *server, uint32_t button, bool pressed) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }
    // A button held from before the capture is released through the normal path
    if (!qw_input_capture_track(capture->pressed_buttons, &capture->pressed_button_count,
                                sizeof(capture->pressed_buttons) / sizeof(uint32_t), button,
                                pressed)) {
        return false;
    }
    if (capture->pointer != NULL) {
        eis_device_button_button(capture->pointer, button, pressed);
    }
    return true;
}

bool qw_input_capture_handle_axis(struct qw_server *server, struct wlr_pointer_axis_event *event) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }
    if (capture->pointer == NULL) {
        return true;
    }

    bool vertical = event->orientation == WL_POINTER_AXIS_VERTICAL_SCROLL;
    if (event->source == WL_POINTER_AXIS_SOURCE_WHEEL) {
        // Wheels send discrete steps only; receivers count both kinds of event
        int32_t steps = event->delta_discrete;
        eis_device_scroll_discrete(capture->pointer, vertical ? 0 : steps, vertical ? steps : 0);
    } else if (event->delta == 0) {
        eis_device_scroll_stop(capture->pointer, !vertical, vertical);
    } else {
        eis_device_scroll_delta(capture->pointer, vertical ? 0 : event->delta,
                                vertical ? event->delta : 0);
    }
    return true;
}

bool qw_input_capture_handle_frame(struct qw_server *server) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }
    if (capture->pointer != NULL) {
        eis_device_frame(capture->pointer, eis_now(capture->eis));
    }
    return true;
}

static bool qw_input_capture_is_release_key(struct qw_server *server, struct qw_keyboard *keyboard,
                                            uint32_t keycode) {
    if (server->input_capture_release_keysym == 0) {
        return false;
    }

    // Lock modifiers are ignored, as for keybindings
    struct wlr_keyboard *wlr_keyboard = keyboard->wlr_keyboard;
    uint32_t ignored = WLR_MODIFIER_CAPS | WLR_MODIFIER_MOD2;
    if ((wlr_keyboard_get_modifiers(wlr_keyboard) & ~ignored) !=
        (server->input_capture_release_modifiers & ~ignored)) {
        return false;
    }

    // Level 0 keysyms, as for keybindings
    xkb_layout_index_t layout = xkb_state_key_get_layout(wlr_keyboard->xkb_state, keycode + 8);
    if (layout == XKB_LAYOUT_INVALID) {
        return false;
    }
    const xkb_keysym_t *syms;
    int nsyms =
        xkb_keymap_key_get_syms_by_level(wlr_keyboard->keymap, keycode + 8, layout, 0, &syms);
    for (int i = 0; i < nsyms; i++) {
        if (syms[i] == server->input_capture_release_keysym) {
            return true;
        }
    }
    return false;
}

bool qw_input_capture_handle_key(struct qw_server *server, struct qw_keyboard *keyboard,
                                 struct wlr_keyboard_key_event *event) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }

    bool pressed = event->state == WL_KEYBOARD_KEY_STATE_PRESSED;
    if (pressed && qw_input_capture_is_release_key(server, keyboard, event->keycode)) {
        wlr_log(WLR_INFO, "Input capture released by key");
        qw_input_capture_deactivate(capture, true, false, 0, 0);
        qw_input_capture_disable_notify(capture);
        return true;
    }

    // Without a keyboard device the keys are swallowed rather than going to Qtile
    if (capture->keyboard == NULL) {
        return true;
    }

    // A key held from before the capture is released through the normal path
    if (!qw_input_capture_track(capture->pressed_keys, &capture->pressed_key_count,
                                sizeof(capture->pressed_keys) / sizeof(uint32_t), event->keycode,
                                pressed)) {
        return false;
    }
    eis_device_keyboard_key(capture->keyboard, event->keycode, pressed);
    eis_device_frame(capture->keyboard, eis_now(capture->eis));
    return true;
}

bool qw_input_capture_handle_modifiers(struct qw_server *server, struct qw_keyboard *keyboard) {
    struct qw_input_capture *capture = server->active_input_capture;
    if (capture == NULL) {
        return false;
    }
    if (capture->keyboard != NULL) {
        struct wlr_keyboard_modifiers *mods = &keyboard->wlr_keyboard->modifiers;
        eis_device_keyboard_send_xkb_modifiers(capture->keyboard, mods->depressed, mods->latched,
                                               mods->locked, mods->group);
    }
    return true;
}

void qw_input_capture_force_release_all(struct qw_server *server) {
    struct qw_input_capture *capture;
    wl_list_for_each(capture, &server->input_captures, link) {
        qw_input_capture_deactivate(capture, true, false, 0, 0);
        qw_input_capture_disable_notify(capture);
    }
}

void qw_input_capture_handle_layout_change(struct qw_server *server) {
    // The layout changes for many reasons; only a change of zones matters here
    struct wlr_box zones[QW_INPUT_CAPTURE_MAX_ZONES];
    size_t count = qw_input_capture_get_zones(server, zones, QW_INPUT_CAPTURE_MAX_ZONES);
    if (count == server->input_capture_zone_count &&
        memcmp(zones, server->input_capture_zones, count * sizeof(*zones)) == 0) {
        return;
    }
    memcpy(server->input_capture_zones, zones, count * sizeof(*zones));
    server->input_capture_zone_count = count;

    struct qw_input_capture *capture;
    wl_list_for_each(capture, &server->input_captures, link) {
        qw_input_capture_deactivate(capture, true, false, 0, 0);
        qw_input_capture_disable_notify(capture);
        qw_input_capture_clear_barriers(capture);

        // Regions follow the outputs
        if (capture->pointer != NULL) {
            qw_input_capture_remove_device(&capture->pointer);
            qw_input_capture_create_pointer(capture);
        }

        if (server->input_capture_zones_changed_cb != NULL) {
            server->input_capture_zones_changed_cb(capture->userdata);
        }
    }
}

void qw_input_capture_handle_keymap_change(struct qw_server *server) {
    struct qw_input_capture *capture;
    wl_list_for_each(capture, &server->input_captures, link) {
        if (!capture->keyboard_bound) {
            continue;
        }
        qw_input_capture_remove_device(&capture->keyboard);
        qw_input_capture_create_keyboard(capture);
    }
}
