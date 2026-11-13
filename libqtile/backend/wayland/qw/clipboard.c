#include "clipboard.h"
#include "server.h"
#include "util.h"

#include <unistd.h>

// Placeholders with the final signatures, so the portal can be written against them; the
// compositor side replaces every one of them.

struct qw_clipboard_source *qw_clipboard_set_selection(struct qw_server *server,
                                                       const char **mime_types,
                                                       size_t mime_type_count, void *userdata) {
    UNUSED(server);
    UNUSED(mime_types);
    UNUSED(mime_type_count);
    UNUSED(userdata);
    return NULL;
}

void qw_clipboard_source_destroy(struct qw_clipboard_source *source) { UNUSED(source); }

bool qw_clipboard_send_selection(struct qw_server *server, const char *mime_type, int fd) {
    UNUSED(server);
    UNUSED(mime_type);
    close(fd);
    return false;
}

size_t qw_clipboard_get_mime_types(struct qw_server *server, const char **mime_types, size_t max) {
    UNUSED(server);
    UNUSED(mime_types);
    UNUSED(max);
    return 0;
}

void *qw_clipboard_get_selection_userdata(struct qw_server *server) {
    UNUSED(server);
    return NULL;
}
