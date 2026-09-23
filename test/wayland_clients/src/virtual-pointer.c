#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "client-base.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

struct test_state {
    struct client_state base;
    struct zwlr_virtual_pointer_manager_v1 *vpointer_mgr;
    struct zwlr_virtual_pointer_v1 *vpointer;
};

static void registry_handler(struct client_state *base, struct wl_registry *registry, uint32_t name,
                             const char *interface, uint32_t version) {
    struct test_state *state = (struct test_state *)base;

    if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
        state->vpointer_mgr =
            wl_registry_bind(registry, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
    }

    if (state->vpointer == NULL && state->vpointer_mgr != NULL && state->base.seat != NULL) {
        state->vpointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(
            state->vpointer_mgr, state->base.seat);
    }
}

static void frame(struct test_state *state) {
    zwlr_virtual_pointer_v1_frame(state->vpointer);
    do_roundtrip(&state->base);
    test_ok();
}

static bool dispatch_command(struct client_state *base, const char *cmd, const char *arg) {
    struct test_state *state = (struct test_state *)base;

    if (strcmp(cmd, "quit") == 0) {
        return false;
    }

    if (state->vpointer == NULL) {
        test_error("no virtual pointer.");
        return true;
    }

    if (strcmp(cmd, "motion") == 0) {
        double dx, dy;
        if (arg == NULL || sscanf(arg, "%lf %lf", &dx, &dy) != 2) {
            test_error("motion requires dx and dy.");
            return true;
        }
        zwlr_virtual_pointer_v1_motion(state->vpointer, 0, wl_fixed_from_double(dx),
                                       wl_fixed_from_double(dy));
        frame(state);
    } else if (strcmp(cmd, "press") == 0 || strcmp(cmd, "release") == 0) {
        if (arg == NULL) {
            test_error("%s requires a button code.", cmd);
            return true;
        }
        uint32_t button_state = strcmp(cmd, "press") == 0 ? WL_POINTER_BUTTON_STATE_PRESSED
                                                          : WL_POINTER_BUTTON_STATE_RELEASED;
        zwlr_virtual_pointer_v1_button(state->vpointer, 0, atoi(arg), button_state);
        frame(state);
    } else if (strcmp(cmd, "wheel") == 0) {
        // Vertical mouse wheel steps
        if (arg == NULL) {
            test_error("wheel requires a number of steps.");
            return true;
        }
        int steps = atoi(arg);
        zwlr_virtual_pointer_v1_axis_source(state->vpointer, WL_POINTER_AXIS_SOURCE_WHEEL);
        zwlr_virtual_pointer_v1_axis_discrete(state->vpointer, 0, WL_POINTER_AXIS_VERTICAL_SCROLL,
                                              wl_fixed_from_int(steps * 15), steps);
        frame(state);
    } else {
        test_error("unknown command: %s", cmd);
    }
    return true;
}

static void cleanup(struct client_state *base) {
    struct test_state *state = (struct test_state *)base;

    if (state->vpointer) {
        zwlr_virtual_pointer_v1_destroy(state->vpointer);
    }
    if (state->vpointer_mgr) {
        zwlr_virtual_pointer_manager_v1_destroy(state->vpointer_mgr);
    }
}

int main(void) {
    struct test_state state = {0};

    const struct client_ops ops = {.registry_global = registry_handler,
                                   .dispatch_command = dispatch_command,
                                   .cleanup = cleanup};

    return client_run(&state.base, &ops);
}
