import logging

import pwnagotchi.plugins as plugins


class UiTweaks(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.3.0'
    __license__ = 'GPL3'
    __description__ = (
        'Small cosmetic rewrites of built-in UI elements: the prompt character after the name, '
        'and the seconds in the uptime counter.'
    )
    __help__ = """
    Both elements are hardcoded in pwnagotchi/ui/view.py, so patching the core would be undone by
    the next auto-update. This rewrites them from on_ui_update instead, which runs before the
    elements are drawn:

        plugins.on('ui_update', self)

        for key, lv in state.items():
            lv.draw(self._canvas, drawer)

    Configuration, both optional:

      [main.plugins.ui_tweaks]
      enabled = true
      name_suffix = "_"        # replaces the '>' after the name, "" removes it
      uptime_seconds = false   # false turns UP 01:23:45 into UP 01:23
      uptime_x_coord = 203     # moves the UP element, omit to leave it alone
      status_y_coord = 38      # moves the status bubble down, omit to leave it alone
      status_x_coord = 130     # moves it right, off the face
      friend_x_coord = 95      # moves the closest peer line into the bottom bar
      friend_y_coord = 110

    Dropping the seconds is worth it when ui.fps is 0: the uptime is in the view's ignore list, so
    it is only repainted when something else changes and the seconds are stale anyway.

        self._ignore_changes = ('uptime', 'name')

    Dropping them also leaves a gap at the right edge, because the layout places UP so that
    HH:MM:SS ends two pixels from the border. On a 250 wide panel the element sits at x 185, the
    value starts 15 pixels in, and each character is 6 wide, so HH:MM:SS ends at 248 and HH:MM at
    230. Setting uptime_x_coord to 203 restores the original right margin.

    status_y_coord lines the first line of the status bubble up with the face. The layout puts the
    bubble at y 20 and the face at 34; with DejaVu metrics (2048 units per em, 1901 ascender) a 35
    pixel face has its baseline at 66 and the parenthesis reaches about 27 pixels above it, so its
    top edge sits near y 40. The status text at 10 pixels starts its capitals roughly 2 pixels
    below its own origin, hence 38.

    Be aware of what this costs: the bubble wraps every 20 characters and grows downwards, so
    moving it 18 pixels down eats the same amount of clearance above whatever sits below it. At
    y 20 a four line message ends at 68; at y 38 it ends at 86, which is where a plugin row placed
    at 84 would be.

    status_x_coord opens a gap between the face and the bubble. DejaVu Sans Mono advances 1233
    units per character, so at 35 pixels a six character face is 126 wide and the layout's x 125
    puts the bubble right on top of the closing parenthesis. There is very little room to give:
    twenty characters at 6 pixels are 120 wide, so 130 ends exactly on the 250 pixel border. Going
    further trades a wider gap for a couple of clipped pixels on full width lines, more with the
    oblique font, whose last glyph leans right.

    friend_x_coord and friend_y_coord move the closest peer line, which the layout puts at (0, 92)
    right under the face, into the empty stretch of the bottom bar. Note that the element is
    'friend_name' and it carries the whole string, signal bars included, because 'friend_face' is
    commented out in view.py and friend_name inherits its position:

        # 'friend_face': Text(value=None, position=self._layout['friend_face'], ...),
        'friend_name': Text(value=None, position=self._layout['friend_face'], font=fonts.BoldSmall, ...),

    The bottom bar holds only PWND on the left and the mode on the right at x 225, so roughly
    x 95 to 225 is free. At BoldSmall a character is about 4.8 pixels wide, which leaves room for
    some 27 characters, enough for four signal bars, a peer name and its two counters.

    The blinking cursor from ui.cursor is preserved: it is stripped before the suffix is replaced
    and put back afterwards.
    """

    def __init__(self):
        self.options = dict()
        self.name_suffix = '_'
        self.uptime_seconds = False
        self.uptime_x = None
        self.status_y = None
        self.status_x = None
        self.friend_x = None
        self.friend_y = None

    def on_loaded(self):
        self.name_suffix = str(self.options.get('name_suffix', '_'))
        self.uptime_seconds = bool(self.options.get('uptime_seconds', False))

        x = self.options.get('uptime_x_coord')
        self.uptime_x = int(x) if x is not None else None

        y = self.options.get('status_y_coord')
        self.status_y = int(y) if y is not None else None

        x = self.options.get('status_x_coord')
        self.status_x = int(x) if x is not None else None

        x = self.options.get('friend_x_coord')
        self.friend_x = int(x) if x is not None else None
        y = self.options.get('friend_y_coord')
        self.friend_y = int(y) if y is not None else None

        logging.info(
            "[ui_tweaks] loaded, name suffix %r, uptime seconds %s, uptime x %s, status %s",
            self.name_suffix, self.uptime_seconds, self.uptime_x,
            (self.status_x, self.status_y),
        )

    def on_ui_setup(self, ui):
        if self.uptime_x is not None:
            self._move(ui, 'uptime', x=self.uptime_x)
        if self.status_x is not None or self.status_y is not None:
            self._move(ui, 'status', x=self.status_x, y=self.status_y)
        if self.friend_x is not None or self.friend_y is not None:
            self._move(ui, 'friend_name', x=self.friend_x, y=self.friend_y)

    def _move(self, ui, key, x=None, y=None):
        # There is no public accessor for a widget, only for its value, so reach
        # into the state the same way the bundled pisugarx plugin does.
        try:
            widget = ui._state._state[key]
        except (AttributeError, KeyError):
            logging.warning("[ui_tweaks] no %s element to move", key)
            return

        widget.xy = (
            widget.xy[0] if x is None else x,
            widget.xy[1] if y is None else y,
        )
        logging.info("[ui_tweaks] %s moved to %s", key, widget.xy)

    def on_ui_update(self, ui):
        self._fix_name(ui)
        if not self.uptime_seconds:
            self._trim_uptime(ui)

    def _fix_name(self, ui):
        name = ui.get('name')
        if not name:
            return

        # ui.cursor appends ' █' on alternate frames, so peel it off before
        # touching the suffix and restore it after.
        cursor = ''
        base = name
        if base.endswith('█'):
            base = base[:-1].rstrip()
            cursor = ' █'

        if base.endswith('>'):
            ui.set('name', base[:-1] + self.name_suffix + cursor)

    def _trim_uptime(self, ui):
        uptime = ui.get('uptime')
        # Only touch the HH:MM:SS form. Once trimmed there is a single colon
        # left, so re-running on the same value is a no-op.
        if uptime and uptime.count(':') == 2:
            ui.set('uptime', uptime.rsplit(':', 1)[0])
