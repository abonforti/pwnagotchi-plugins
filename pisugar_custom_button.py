import logging
import threading

import pwnagotchi.plugins as plugins

# PiSugar 3 MCU
I2C_BUS = 1
I2C_ADDR = 0x57

REG_TAP = 0x08           # bits 0-1 latch the gesture until cleared
REG_WRITE_ENABLE = 0x0B  # 0x29 unlocks the other registers, anything else locks

WRITE_UNLOCK = 0x29
WRITE_LOCK = 0x00
TAP_MASK = 0b11

# Same encoding pisugar-server uses in pisugar-core/src/pisugar3.rs
TAPS = {1: 'single', 2: 'double', 3: 'long'}


class PiSugarCustomButton(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = (
        'Reads the PiSugar 3 custom button and emits a pwnagotchi plugin event for each gesture.'
    )
    __help__ = """
    The custom button is the one on the edge nearest the Type-C port. Nothing reads it out of the
    box: the bundled pisugarx plugin has the tap getters as empty stubs, and the vendor's
    pisugar-server cannot be run alongside it because both would poll the same I2C device.

    The MCU latches the gesture in register 0x08 and keeps it there until it is cleared, so a two
    second poll never misses one. The encoding matches pisugar-core:

        1 => single, 2 => double, 3 => long

    A gesture is turned into a plugin event and nothing else:

        plugins.on(action)

    which reaches any plugin implementing on_<action>. This plugin therefore knows nothing about
    what it triggers, and adding a behaviour later means writing the plugin that handles the
    event, not touching this one.

    Configuration:

      [main.plugins.pisugar_custom_button]
      enabled = true
      poll_interval = 2
      single = "none"
      double = "none"
      long = "pasv_toggle"

    Only the long press is bound at the moment, to the passive mode in pasv_mode.py. The other two
    are wired but left unbound on purpose: a short press is easy to trigger by accident in a
    pocket, and the actions worth having there have not been decided.

    Events are dispatched on the target plugin's own thread and return nothing, so a binding
    cannot report success here. Look at the log of whatever handles it.

    Runs alongside pisugar_power_button, which owns the power button. Both lift the write
    protection on 0x0B around a write, so in principle a tap and a shutdown landing in the same
    instant could interleave. Writes are rare enough on both sides that this has not been worth
    guarding against.
    """

    def __init__(self):
        self.options = dict()
        self.poll_interval = 2
        self.bindings = {}
        self._bus = None
        self._stop = threading.Event()
        self._thread = None

    # --- i2c ---------------------------------------------------------------

    def _read(self, reg):
        return self._bus.read_byte_data(I2C_ADDR, reg)

    def _write(self, reg, value):
        self._bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_UNLOCK)
        try:
            self._bus.write_byte_data(I2C_ADDR, reg, value)
        finally:
            self._bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_LOCK)

    def _read_tap(self):
        tap = self._read(REG_TAP) & TAP_MASK
        if tap:
            self._write(REG_TAP, self._read(REG_TAP) & ~TAP_MASK & 0xFF)
        return TAPS.get(tap)

    # --- dispatch ----------------------------------------------------------

    def _dispatch(self, tap):
        action = self.bindings.get(tap, 'none')
        if action == 'none':
            logging.debug("[pisugar_custom_button] %s tap, nothing bound", tap)
            return

        logging.info("[pisugar_custom_button] %s tap -> %s", tap, action)
        try:
            plugins.on(action)
        except Exception as e:
            logging.error("[pisugar_custom_button] %s failed: %s", action, e)

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        self.poll_interval = max(1, int(self.options.get('poll_interval', 2)))

        defaults = {'single': 'none', 'double': 'none', 'long': 'pasv_toggle'}
        for tap, default in defaults.items():
            self.bindings[tap] = str(self.options.get(tap, default)).strip() or 'none'

        bound = {k: v for k, v in self.bindings.items() if v != 'none'}
        if not bound:
            logging.info("[pisugar_custom_button] loaded, nothing bound")
            return

        try:
            from smbus2 import SMBus
            self._bus = SMBus(I2C_BUS)
        except Exception as e:
            logging.error("[pisugar_custom_button] cannot open i2c bus %d: %s", I2C_BUS, e)
            return

        logging.info("[pisugar_custom_button] loaded, poll every %ds, bound: %s",
                     self.poll_interval, bound)

        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def on_unload(self, ui):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    # --- polling -----------------------------------------------------------

    def _poll(self):
        while not self._stop.is_set():
            try:
                tap = self._read_tap()
                if tap:
                    self._dispatch(tap)
            except OSError as e:
                # pisugarx polls the same device, so the odd collision is normal.
                logging.debug("[pisugar_custom_button] i2c failed: %s", e)

            self._stop.wait(self.poll_interval)
