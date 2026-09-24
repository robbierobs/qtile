from libqtile import bar
from libqtile.log_utils import logger
from libqtile.widget import base
from libqtile.widget.helpers.status_notifier import StatusNotifierItem, has_xdg, host


class StatusNotifier(base._Widget):
    """
    A 'system tray' widget using the freedesktop StatusNotifierItem
    specification.

    As per the specification, app icons are first retrieved from the
    user's current theme. If this is not available then the app may
    provide its own icon. In order to use this functionality, users
    are recommended to install the `pyxdg <https://pypi.org/project/pyxdg/>`__
    module to support retrieving icons from the selected theme.
    If the icon specified by StatusNotifierItem can not be found in
    the user's current theme and no other icons are provided by the
    app, a fallback icon is used.

    Left-clicking an icon will trigger an activate event.

    .. note::

        Context menus are not currently supported by the official widget.
        However, a modded version of the widget which provides basic menu
        support is available from elParaguayo's `qtile-extras
        <https://github.com/elParaguayo/qtile-extras>`_ repo.
    """

    orientations = base.ORIENTATION_BOTH

    defaults = [
        ("icon_size", 16, "Icon width"),
        ("icon_theme", None, "Name of theme to use for app icons"),
        ("padding", 3, "Padding between icons"),
    ]

    def __init__(self, **config):
        base._Widget.__init__(self, bar.CALCULATED, **config)
        self.add_defaults(StatusNotifier.defaults)
        # Icons are decoded in a worker thread: decoding an SVG or a large
        # pixmap in the event loop stalls input and rendering, and any bar
        # redraw can land on an icon that just changed. Keyed by id(item)
        # (items aren't hashable). item.images is replaced whenever the app
        # sends a new icon, so it identifies which icon a decode belongs to:
        #   _ready_icons: id -> (item.images, decoded icon)
        #   _preparing:   id -> item.images being decoded
        #   _failed:      id -> item.images whose decode failed (not retried)
        self._ready_icons: dict[int, tuple[dict, object]] = {}
        self._preparing: dict[int, dict] = {}
        self._failed: dict[int, dict] = {}
        self.add_callbacks(
            {
                "Button1": self.activate,
            }
        )
        self.selected_item: StatusNotifierItem | None = None

    @property
    def available_icons(self):
        return [item for item in host.items if item.has_icons]

    def calculate_length(self):
        if not host.items:
            return 0

        return len(self.available_icons) * (self.icon_size + self.padding) + self.padding

    def _configure(self, qtile, bar):
        if has_xdg and self.icon_theme:
            host.icon_theme = self.icon_theme

        # This is called last as it starts timers including _config_async.
        base._Widget._configure(self, qtile, bar)

    def draw_callback(self, x=None):
        self.bar.draw()

    async def _config_async(self):
        await host.start(
            on_item_added=self.draw_callback,
            on_item_removed=self.draw_callback,
            on_icon_changed=self.draw_callback,
        )

    def find_icon_at_pos(self, x, y):
        """returns StatusNotifierItem object for icon in given position"""
        offset = self.padding
        val = x if self.bar.horizontal else y

        if val < offset:
            return None

        for icon in self.available_icons:
            offset += self.icon_size
            if val < offset:
                return icon
            offset += self.padding

        return None

    def button_press(self, x, y, button):
        icon = self.find_icon_at_pos(x, y)
        self.selected_item = icon if icon else None

        name = f"Button{button}"
        if name in self.mouse_callbacks:
            self.mouse_callbacks[name]()

    def _draw_icon(self, icon, x, y):
        self.drawer.draw_image(icon, x, y)

    def draw(self):
        self.drawer.clear(self.background or self.bar.background)
        xoffset = self.padding
        yoffset = (self.bar.size - self.icon_size) // 2

        # Scale icon up by output scale factor
        scaled_icon_size = int(self.icon_size * self.drawer.output_scale)
        items = self.available_icons
        current = {id(item) for item in items}
        for state in (self._ready_icons, self._preparing, self._failed):
            for key in state.keys() - current:
                del state[key]

        for item in items:
            # Until its first decode finishes an item's slot stays empty
            icon = self._ready_icon(item, scaled_icon_size)
            if icon is not None:
                if self.bar.horizontal:
                    self._draw_icon(icon, xoffset, yoffset)
                else:
                    self._draw_icon(icon, yoffset, xoffset)
            xoffset += self.icon_size + self.padding

        self.draw_at_default_position()

    def _ready_icon(self, item, size):
        """The item's decoded icon. While a new one decodes, the previous one."""
        key = id(item)
        images, icon = self._ready_icons.get(key, (None, None))
        if images is item.images and size in item.images:
            return icon
        if (
            self._preparing.get(key) is not item.images
            and self._failed.get(key) is not item.images
        ):
            self._preparing[key] = item.images
            future = self.qtile.run_in_executor(self._prepare_icon, item, size)
            future.add_done_callback(
                lambda f, item=item, images=item.images: self._icon_prepared(
                    item, images, size, f
                )
            )
        return icon

    def _prepare_icon(self, item, size):
        """Runs in a worker thread: build the icon and decode it at the size
        it's drawn at."""
        # Get the icon at its scaled size or larger (if possible)
        icon = item.build_icon(size)
        icon.resize(height=self.icon_size)
        icon.pattern  # noqa: B018 - decodes the image and caches the result
        return icon

    def _icon_prepared(self, item, images, size, future):
        key = id(item)
        if self._preparing.get(key) is images:
            del self._preparing[key]
        if self.finalized:
            return
        try:
            icon = future.result()
        except Exception:
            logger.exception("Error decoding icon for StatusNotifierItem %s", item.service)
            self._failed[key] = images
            return
        # The app may have sent another icon meanwhile; then this one is stale
        # and the redraw below starts decoding the new one
        if images is item.images:
            images[size] = icon
            self._ready_icons[key] = (images, icon)
        self.bar.draw()

    def activate(self):
        """Primary action when clicking on an icon"""
        if not self.selected_item:
            return
        self.selected_item.activate()

    def finalize(self):
        host.unregister_callbacks(
            on_item_added=self.draw_callback,
            on_item_removed=self.draw_callback,
            on_icon_changed=self.draw_callback,
        )
        base._Widget.finalize(self)
