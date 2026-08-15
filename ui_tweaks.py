import logging
import threading

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import Text
from pwnagotchi.ui.view import BLACK


class UiTweaks(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.6.0'
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
      net_x_coord = 82         # connectivity indicator, omit to disable it entirely
      net_y_coord = 20
      net_up_text = "UP"
      net_down_text = "DOWN"
      net_url = "http://cp.cloudflare.com/generate_204"
      net_interval = 60
      net_timeout = 3
      net_font = "Bold"
      friend_font = "Medium"   # match the PWND value instead of the smaller default
      friend_preview = "<bars> peer 3 (12)"   # layout aid, remove afterwards

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
    x 95 to 225 is free.

    friend_font names an attribute of pwnagotchi.ui.fonts, so Small, Medium, Bold, BoldSmall,
    BoldBig or Huge. The peer line is drawn in BoldSmall at 8 pixels while the PWND value next to
    it uses Medium at 10, which is why it looks undersized down there; 'Medium' matches it. Mind
    the width when you enlarge it: at 10 pixels a character is 6 wide instead of 4.8, so a
    seventeen character peer line grows from 82 to 102 pixels, and a long peer name can reach the
    mode indicator at 225.

    The connectivity indicator is a bare Text, not a LabeledValue: 'UP' and 'DOWN' already say
    what they mean and a label would need room this row does not have. It only exists when both
    net coordinates are configured, and only then is the polling thread started.

    Nothing else on screen answers the question. BT reports the Bluetooth link, and even its 'C'
    state only means the network profile is up: a phone with no signal or with mobile data off
    still reads BT C. bt-tether can test connectivity but only exposes it through its web page.

    grid.is_connected() is deliberately not reused, because it waits up to 30 seconds to connect
    and 60 to read, and it answers 'no internet' whenever api.opwngrid.xyz itself is down. This
    uses a captive portal probe instead, which returns 204 with no body.

    The default endpoint is Cloudflare rather than the more common gstatic one, since this is a
    device you carry around and the probe is a beacon that repeats on every interval. Point
    net_url wherever you prefer; note that aiming it at a host on your own tailnet tells you the
    tunnel is up, not that the internet is.

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
        self.friend_preview = None
        self.friend_font = None
        self.net_position = None
        self.net_font = 'Bold'
        self.net_up_text = 'UP'
        self.net_down_text = 'DOWN'
        self.net_url = 'http://cp.cloudflare.com/generate_204'
        self.net_interval = 60
        self.net_timeout = 3
        self._online = False
        self._stop = threading.Event()
        self._thread = None

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
        self.friend_preview = self.options.get('friend_preview') or None
        self.friend_font = self.options.get('friend_font') or None

        x = self.options.get('net_x_coord')
        y = self.options.get('net_y_coord')
        self.net_position = (int(x), int(y)) if x is not None and y is not None else None
        self.net_font = self.options.get('net_font', self.net_font)
        self.net_up_text = self.options.get('net_up_text', self.net_up_text)
        self.net_down_text = self.options.get('net_down_text', self.net_down_text)
        self.net_url = self.options.get('net_url', self.net_url)
        self.net_interval = max(10, int(self.options.get('net_interval', 60)))
        self.net_timeout = max(1, int(self.options.get('net_timeout', 3)))

        logging.info(
            "[ui_tweaks] loaded, name suffix %r, uptime seconds %s, uptime x %s, status %s",
            self.name_suffix, self.uptime_seconds, self.uptime_x,
            (self.status_x, self.status_y),
        )

        if self.net_position is not None:
            logging.info(
                "[ui_tweaks] connectivity check every %ds against %s",
                self.net_interval, self.net_url,
            )
            self._stop.clear()
            self._thread = threading.Thread(target=self._watch_net, daemon=True)
            self._thread.start()

    def on_unload(self, ui):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.net_timeout + 1)
        if self.net_position is not None:
            with ui._lock:
                ui.remove_element('netstatus')

    def on_ui_setup(self, ui):
        if self.uptime_x is not None:
            self._move(ui, 'uptime', x=self.uptime_x)
        if self.status_x is not None or self.status_y is not None:
            self._move(ui, 'status', x=self.status_x, y=self.status_y)
        if self.friend_x is not None or self.friend_y is not None:
            self._move(ui, 'friend_name', x=self.friend_x, y=self.friend_y)
        if self.friend_font is not None:
            self._restyle(ui, 'friend_name', self.friend_font)

        if self.net_position is not None:
            # A bare Text rather than a LabeledValue: the words already say what
            # they mean, and a label would need room the row does not have.
            ui.add_element(
                'netstatus',
                Text(
                    color=BLACK,
                    value=self.net_down_text,
                    position=self.net_position,
                    font=getattr(fonts, self.net_font, fonts.Bold),
                ),
            )

    def _restyle(self, ui, key, font_name):
        font = getattr(fonts, font_name, None)
        if font is None:
            logging.warning("[ui_tweaks] unknown font %r", font_name)
            return
        try:
            ui._state._state[key].font = font
        except (AttributeError, KeyError):
            logging.warning("[ui_tweaks] no %s element to restyle", key)
            return
        logging.info("[ui_tweaks] %s font set to %s", key, font_name)

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
        if self.net_position is not None and 'netstatus' in ui._state._state:
            ui.set('netstatus', self.net_up_text if self._online else self.net_down_text)
        if self.friend_preview:
            # Layout aid only. The peer line is normally empty until another
            # pwnagotchi is in range, which makes its placement impossible to
            # check with a single unit.
            ui.set('friend_name', self.friend_preview)

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

    def _watch_net(self):
        import requests

        while not self._stop.is_set():
            online = False
            try:
                # A captive portal probe: 204 with no body, so the answer is a
                # status code rather than a page to download.
                response = requests.get(self.net_url, timeout=self.net_timeout)
                online = response.status_code in (200, 204)
            except Exception as e:
                logging.debug("[ui_tweaks] connectivity check failed: %s", e)

            if online != self._online:
                logging.info("[ui_tweaks] connectivity %s", "up" if online else "down")
                self._online = online

            self._stop.wait(self.net_interval)

    def _trim_uptime(self, ui):
        uptime = ui.get('uptime')
        # Only touch the HH:MM:SS form. Once trimmed there is a single colon
        # left, so re-running on the same value is a no-op.
        if uptime and uptime.count(':') == 2:
            ui.set('uptime', uptime.rsplit(':', 1)[0])
