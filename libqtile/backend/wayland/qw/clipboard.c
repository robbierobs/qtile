#include "clipboard.h"
#include "server.h"

#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-util.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_seat.h>

struct qw_clipboard_source {
    struct wlr_data_source base;
    struct qw_server *server;
    void *userdata;
};

static void source_send(struct wlr_data_source *base, const char *mime_type, int32_t fd);
static void source_destroy(struct wlr_data_source *base);

static const struct wlr_data_source_impl source_impl = {
    .send = source_send,
    .destroy = source_destroy,
};

static void source_send(struct wlr_data_source *base, const char *mime_type, int32_t fd) {
    struct qw_clipboard_source *source = wl_container_of(base, source, base);
    if (source->server->clipboard_source_send_cb == NULL) {
        close(fd);
        return;
    }
    source->server->clipboard_source_send_cb(source->userdata, mime_type, fd);
}

// wlroots has already freed the mime types by the time this runs.
static void source_destroy(struct wlr_data_source *base) {
    struct qw_clipboard_source *source = wl_container_of(base, source, base);
    if (source->server->clipboard_source_cancelled_cb != NULL) {
        source->server->clipboard_source_cancelled_cb(source->userdata);
    }
    free(source);
}

// Clients are otherwise flushed only when the display is dispatched, which these calls, made
// from the portal rather than from a client request, are not.
static void flush_clients(struct qw_server *server) { wl_display_flush_clients(server->display); }

static bool source_offers(struct wlr_data_source *source, const char *mime_type) {
    char **p;
    wl_array_for_each(p, &source->mime_types) {
        if (strcmp(*p, mime_type) == 0) {
            return true;
        }
    }
    return false;
}

struct qw_clipboard_source *qw_clipboard_set_selection(struct qw_server *server,
                                                       const char **mime_types,
                                                       size_t mime_type_count, void *userdata) {
    if (mime_type_count == 0) {
        return NULL;
    }

    struct qw_clipboard_source *source = calloc(1, sizeof(*source));
    if (source == NULL) {
        return NULL;
    }
    source->server = server;
    source->userdata = userdata;
    wlr_data_source_init(&source->base, &source_impl);

    for (size_t i = 0; i < mime_type_count; i++) {
        char **p = wl_array_add(&source->base.mime_types, sizeof(*p));
        if (p != NULL) {
            *p = strdup(mime_types[i]);
        }
        if (p == NULL || *p == NULL) {
            char **q;
            wl_array_for_each(q, &source->base.mime_types) { free(*q); }
            wl_array_release(&source->base.mime_types);
            free(source);
            return NULL;
        }
    }

    wlr_seat_set_selection(server->seat, &source->base, wl_display_next_serial(server->display));
    flush_clients(server);
    return source;
}

void qw_clipboard_source_destroy(struct qw_clipboard_source *source) {
    struct wlr_seat *seat = source->server->seat;
    if (seat->selection_source == &source->base) {
        wlr_seat_set_selection(seat, NULL, wl_display_next_serial(source->server->display));
    } else {
        wlr_data_source_destroy(&source->base);
    }
    flush_clients(source->server);
}

bool qw_clipboard_send_selection(struct qw_server *server, const char *mime_type, int fd) {
    struct wlr_data_source *selection = server->seat->selection_source;
    if (selection != NULL && source_offers(selection, mime_type)) {
        wlr_data_source_send(selection, mime_type, fd);
        flush_clients(server);
        return true;
    }
    close(fd);
    return false;
}

size_t qw_clipboard_get_mime_types(struct qw_server *server, const char **mime_types, size_t max) {
    struct wlr_data_source *selection = server->seat->selection_source;
    if (selection == NULL) {
        return 0;
    }

    size_t count = 0;
    char **p;
    wl_array_for_each(p, &selection->mime_types) {
        if (count < max) {
            mime_types[count] = *p;
        }
        count++;
    }
    return count;
}

void *qw_clipboard_get_selection_userdata(struct qw_server *server) {
    struct wlr_data_source *selection = server->seat->selection_source;
    if (selection == NULL || selection->impl != &source_impl) {
        return NULL;
    }
    struct qw_clipboard_source *source = wl_container_of(selection, source, base);
    return source->userdata;
}
