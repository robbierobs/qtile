#ifndef QW_CLIPBOARD_H
#define QW_CLIPBOARD_H

#include <stdbool.h>
#include <stddef.h>

struct qw_server;

// Compositor side of the xdg-desktop-portal Clipboard interface. The portal, which lives in
// Python, can make the seat's clipboard selection a source whose data a portal session
// supplies only when a client pastes, and can read whatever the current selection offers.
//
// A source calls the server's clipboard_source_send_cb with its userdata when a client
// pastes: the callee owns the fd, writes the data for the mime type into it and closes it.
// Once the source stops being the selection, whether replaced, cleared or destroyed, the
// server's clipboard_source_cancelled_cb is called exactly once with its userdata and the
// source is freed. The server's clipboard_selection_changed_cb is called with cb_data after
// every change of the selection, including one to or from a source, and after any
// cancellation it caused.
struct qw_clipboard_source;

// Makes a new source offering the mime types the seat's clipboard selection, replacing the
// current one. The mime types are copied. Returns NULL if there are none.
struct qw_clipboard_source *qw_clipboard_set_selection(struct qw_server *server,
                                                       const char **mime_types,
                                                       size_t mime_type_count, void *userdata);

// Clears the selection if the source is still it. The source is cancelled as above before
// this returns, so it must not be used afterwards.
void qw_clipboard_source_destroy(struct qw_clipboard_source *source);

// Asks the current selection to write its data for the mime type into fd, which this takes
// ownership of in every case. Returns false, closing fd, if there is no selection or it does
// not offer the mime type.
bool qw_clipboard_send_selection(struct qw_server *server, const char *mime_type, int fd);

// Fills mime_types with up to max of the current selection's mime types and returns how many
// it offers in all, 0 if there is no selection. The strings belong to the selection: copy
// them before it changes.
size_t qw_clipboard_get_mime_types(struct qw_server *server, const char **mime_types, size_t max);

// The userdata of the current selection if it is a qw_clipboard_source, or NULL
void *qw_clipboard_get_selection_userdata(struct qw_server *server);

#endif /* QW_CLIPBOARD_H */
