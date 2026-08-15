import logging

import pwnagotchi.plugins as plugins


class UiTweaks(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.1.0'
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

    Dropping the seconds is worth it when ui.fps is 0: the uptime is in the view's ignore list, so
    it is only repainted when something else changes and the seconds are stale anyway.

        self._ignore_changes = ('uptime', 'name')

    Dropping them also leaves a gap at the right edge, because the layout places UP so that
    HH:MM:SS ends two pixels from the border. On a 250 wide panel the element sits at x 185, the
    value starts 15 pixels in, and each character is 6 wide, so HH:MM:SS ends at 248 and HH:MM at
    230. Setting uptime_x_coord to 203 restores the original right margin.

    The blinking cursor from ui.cursor is preserved: it is stripped before the suffix is replaced
    and put back afterwards.
    """

    def __init__(self):
        self.options = dict()
        self.name_suffix = '_'
        self.uptime_seconds = False
        self.uptime_x = None

    def on_loaded(self):
        self.name_suffix = str(self.options.get('name_suffix', '_'))
        self.uptime_seconds = bool(self.options.get('uptime_seconds', False))

        x = self.options.get('uptime_x_coord')
        self.uptime_x = int(x) if x is not None else None

        logging.info(
            "[ui_tweaks] loaded, name suffix %r, uptime seconds %s, uptime x %s",
            self.name_suffix, self.uptime_seconds, self.uptime_x,
        )

    def on_ui_setup(self, ui):
        if self.uptime_x is None:
            return

        # There is no public accessor for a widget, only for its value, so reach
        # into the state the same way the bundled pisugarx plugin does.
        try:
            widget = ui._state._state['uptime']
        except (AttributeError, KeyError):
            logging.warning("[ui_tweaks] no uptime element to move")
            return

        widget.xy = (self.uptime_x, widget.xy[1])
        logging.info("[ui_tweaks] uptime moved to x %d", self.uptime_x)

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
