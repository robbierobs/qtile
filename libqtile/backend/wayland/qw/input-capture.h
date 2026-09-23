#ifndef QW_INPUT_CAPTURE_H
#define QW_INPUT_CAPTURE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <wlr/util/box.h>

#define QW_INPUT_CAPTURE_MAX_ZONES 16

struct qw_server;
struct qw_keyboard;
struct wlr_keyboard_key_event;
struct wlr_pointer_axis_event;

// Compositor side of the xdg-desktop-portal InputCapture interface. An input capture
// session hands input to a libei receiver (e.g. Synergy/Deskflow) over EIS: once enabled,
// moving the pointer across one of its barriers activates the capture, and pointer and
// keyboard input then go to the receiver instead of Qtile until the session releases it.
// One session is created per portal session; the portal itself lives in Python.
struct qw_input_capture_barrier {
    uint32_t id;
    int x1, y1, x2, y2; // x1 <= x2 and y1 <= y2, with x1 == x2 or y1 == y2
};

struct qw_input_capture {
    // Private data
    struct qw_server *server;
    void *userdata;      // passed to the server's input_capture_*_cb callbacks
    struct wl_list link; // qw_server.input_captures

    struct eis *eis;
    struct wl_event_source *eis_source;
    bool eis_connected;
    struct eis_client *client;
    struct eis_seat *seat;
    struct eis_device *pointer;
    struct eis_device *keyboard;
    bool keyboard_bound;

    struct qw_input_capture_barrier *barriers;
    size_t barrier_count;

    bool enabled;
    bool active;
    uint32_t activation_id;

    // Keys and buttons pressed while active, which must be released before deactivating
    uint32_t pressed_keys[32];
    size_t pressed_key_count;
    uint32_t pressed_buttons[16];
    size_t pressed_button_count;
};

struct qw_input_capture *qw_input_capture_create(struct qw_server *server, void *userdata);
void qw_input_capture_destroy(struct qw_input_capture *capture);

// Returns the client end of the EIS connection for the portal to hand over, or -1.
// The caller owns the fd.
int qw_input_capture_connect_eis(struct qw_input_capture *capture);

// Barriers are validated against the zones by the portal before being added
void qw_input_capture_clear_barriers(struct qw_input_capture *capture);
bool qw_input_capture_add_barrier(struct qw_input_capture *capture, uint32_t id, int x1, int y1,
                                  int x2, int y2);

void qw_input_capture_enable(struct qw_input_capture *capture);
void qw_input_capture_disable(struct qw_input_capture *capture);
void qw_input_capture_release(struct qw_input_capture *capture, uint32_t activation_id,
                              bool has_position, double x, double y);

// Fills zones with the outputs in layout coordinates, as the portal and EIS regions
// describe them, and returns how many there are
size_t qw_input_capture_get_zones(struct qw_server *server, struct wlr_box *zones, size_t max);

// Key that always ends an active capture, checked before input is forwarded
void qw_server_set_input_capture_release_key(struct qw_server *server, uint32_t keysym,
                                             uint32_t modifiers);

// Hooks for the input paths. Each returns true if the event was consumed by a capture.
bool qw_input_capture_handle_motion(struct qw_server *server, double dx, double dy);
bool qw_input_capture_check_barriers(struct qw_server *server, double x, double y, double dx,
                                     double dy);
bool qw_input_capture_handle_button(struct qw_server *server, uint32_t button, bool pressed);
bool qw_input_capture_handle_axis(struct qw_server *server, struct wlr_pointer_axis_event *event);
bool qw_input_capture_handle_frame(struct qw_server *server);
bool qw_input_capture_handle_key(struct qw_server *server, struct qw_keyboard *keyboard,
                                 struct wlr_keyboard_key_event *event);
bool qw_input_capture_handle_modifiers(struct qw_server *server, struct qw_keyboard *keyboard);

// Ends any active capture and disables every session, e.g. when the session locks
void qw_input_capture_force_release_all(struct qw_server *server);
// Outputs changed: barriers no longer match the zones
void qw_input_capture_handle_layout_change(struct qw_server *server);
// A keyboard's keymap changed; EIS keymaps are immutable so the device is recreated
void qw_input_capture_handle_keymap_change(struct qw_server *server);

#endif /* QW_INPUT_CAPTURE_H */
