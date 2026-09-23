// A libei receiver standing in for an input capture client such as Synergy/Deskflow.
//
// Usage: ei-receiver <fd>, where fd is the client end of an EIS connection. Commands are
// read from stdin, one per line:
//   status   prints the bound devices, keymap and regions
//   events   prints the events received since the last call, one per line
//   quit
// Each command ends with "OK".

#define _GNU_SOURCE

#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libei.h>

#define MAX_EVENTS 256

static struct ei_device *pointer, *keyboard;
static bool resumed_pointer, resumed_keyboard;
static char *events[MAX_EVENTS];
static size_t event_count;

static void record(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void record(const char *fmt, ...) {
    if (event_count == MAX_EVENTS) {
        return;
    }
    va_list args;
    va_start(args, fmt);
    if (vasprintf(&events[event_count], fmt, args) >= 0) {
        event_count++;
    }
    va_end(args);
}

static void print_status(void) {
    printf("pointer: %s\n", pointer != NULL && resumed_pointer ? "resumed" : "none");
    printf("keyboard: %s\n", keyboard != NULL && resumed_keyboard ? "resumed" : "none");
    printf("keymap: %s\n",
           keyboard != NULL && ei_device_keyboard_get_keymap(keyboard) != NULL ? "yes" : "no");
    if (pointer != NULL) {
        struct ei_region *region;
        for (size_t i = 0; (region = ei_device_get_region(pointer, i)) != NULL; i++) {
            printf("region: %ux%u+%u+%u\n", ei_region_get_width(region),
                   ei_region_get_height(region), ei_region_get_x(region), ei_region_get_y(region));
        }
    }
}

static void handle_event(struct ei_event *event) {
    struct ei_device *device = ei_event_get_device(event);

    switch (ei_event_get_type(event)) {
    case EI_EVENT_SEAT_ADDED:
        // As Deskflow does
        ei_seat_bind_capabilities(ei_event_get_seat(event), EI_DEVICE_CAP_POINTER,
                                  EI_DEVICE_CAP_POINTER_ABSOLUTE, EI_DEVICE_CAP_KEYBOARD,
                                  EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_SCROLL, NULL);
        break;
    case EI_EVENT_DEVICE_ADDED:
        if (ei_device_has_capability(device, EI_DEVICE_CAP_POINTER)) {
            pointer = ei_device_ref(device);
            resumed_pointer = false;
        } else if (ei_device_has_capability(device, EI_DEVICE_CAP_KEYBOARD)) {
            keyboard = ei_device_ref(device);
            resumed_keyboard = false;
        }
        break;
    case EI_EVENT_DEVICE_REMOVED:
        if (device == pointer) {
            pointer = ei_device_unref(pointer);
        } else if (device == keyboard) {
            keyboard = ei_device_unref(keyboard);
        }
        break;
    case EI_EVENT_DEVICE_RESUMED:
        if (device == pointer) {
            resumed_pointer = true;
        } else if (device == keyboard) {
            resumed_keyboard = true;
        }
        break;
    case EI_EVENT_DEVICE_START_EMULATING:
        record("start %s %u", device == pointer ? "pointer" : "keyboard",
               ei_event_emulating_get_sequence(event));
        break;
    case EI_EVENT_DEVICE_STOP_EMULATING:
        record("stop %s", device == pointer ? "pointer" : "keyboard");
        break;
    case EI_EVENT_POINTER_MOTION:
        record("motion %.0f %.0f", ei_event_pointer_get_dx(event), ei_event_pointer_get_dy(event));
        break;
    case EI_EVENT_BUTTON_BUTTON:
        record("button %u %s", ei_event_button_get_button(event),
               ei_event_button_get_is_press(event) ? "press" : "release");
        break;
    case EI_EVENT_SCROLL_DISCRETE:
        record("scroll_discrete %d %d", ei_event_scroll_get_discrete_dx(event),
               ei_event_scroll_get_discrete_dy(event));
        break;
    case EI_EVENT_SCROLL_DELTA:
        record("scroll_delta %.0f %.0f", ei_event_scroll_get_dx(event),
               ei_event_scroll_get_dy(event));
        break;
    case EI_EVENT_KEYBOARD_KEY:
        record("key %u %s", ei_event_keyboard_get_key(event),
               ei_event_keyboard_get_key_is_press(event) ? "press" : "release");
        break;
    case EI_EVENT_DISCONNECT:
        record("disconnect");
        break;
    default:
        break;
    }
}

static bool handle_command(const char *line) {
    if (strcmp(line, "quit") == 0) {
        return false;
    }
    if (strcmp(line, "status") == 0) {
        print_status();
    } else if (strcmp(line, "events") == 0) {
        for (size_t i = 0; i < event_count; i++) {
            printf("%s\n", events[i]);
            free(events[i]);
        }
        event_count = 0;
    } else {
        printf("ERROR: unknown command: %s\n", line);
    }
    printf("OK\n");
    fflush(stdout);
    return true;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <fd>\n", argv[0]);
        return 1;
    }

    struct ei *ei = ei_new_receiver(NULL);
    ei_configure_name(ei, "qtile test receiver");
    if (ei_setup_backend_fd(ei, atoi(argv[1])) != 0) {
        fprintf(stderr, "failed to connect to EIS\n");
        return 1;
    }

    struct pollfd fds[2] = {
        {.fd = STDIN_FILENO, .events = POLLIN},
        {.fd = ei_get_fd(ei), .events = POLLIN},
    };
    char line[256];
    size_t pos = 0;
    bool running = true;

    while (running && poll(fds, 2, -1) >= 0) {
        if (fds[1].revents & POLLIN) {
            ei_dispatch(ei);
            struct ei_event *event;
            while ((event = ei_get_event(ei)) != NULL) {
                handle_event(event);
                ei_event_unref(event);
            }
        }
        if (fds[0].revents & (POLLIN | POLLHUP)) {
            ssize_t n = read(STDIN_FILENO, line + pos, sizeof(line) - pos - 1);
            if (n <= 0) {
                break;
            }
            pos += n;
            line[pos] = '\0';
            char *start = line, *nl;
            while (running && (nl = strchr(start, '\n')) != NULL) {
                *nl = '\0';
                running = handle_command(start);
                start = nl + 1;
            }
            pos = strlen(start);
            memmove(line, start, pos + 1);
        }
    }

    ei_device_unref(pointer);
    ei_device_unref(keyboard);
    ei_unref(ei);
    return 0;
}
