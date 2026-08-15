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

    The long press switches between AUTO and PASV, by emitting a plugin event rather than doing
    the work here:

        plugins.on(self.event)

    which reaches any plugin implementing on_<event>, pasv_mode.py in this case. Nothing about
    what passive mode means lives in this file: this one reads a button.

    It is inert in manual mode. A manual pwnagotchi transmits nothing to begin with, so there is
    nothing to switch off, and the display would be announcing a state that means nothing there.
    The press is logged and ignored.

    The single and double branches exist and are reached, but carry no logic yet. A short press is
    easy to trigger by accident in a pocket, and what deserves to sit there has not been decided.

    Configuration:

      [main.plugins.pisugar_custom_button]
      enabled = true
      poll_interval = 2
      event = "pasv_toggle"

    Events are dispatched on the target plugin's own thread and return nothing, so nothing can be
    reported back here. Look at the log of whatever handles it: pasv_mode prints what actually
    changed, including the HTTP status of the call that stops the mesh.

    Runs alongside pisugar_power_button, which owns the power button. Both lift the write
    protection on 0x0B around a write, so in principle a tap and a shutdown landing in the same
    instant could interleave. Writes are rare enough on both sides that this has not been worth
    guarding against.
    """

    def __init__(self):
        self.options = dict()
        self.poll_interval = 2
        self.event = 'pasv_toggle'
        self._bus = None
        self._agent = None
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
        if tap == 'single':
            # Reserved. Nothing bound yet.
            logging.debug("[pisugar_custom_button] single tap, nothing bound")

        elif tap == 'double':
            # Reserved. Nothing bound yet.
            logging.debug("[pisugar_custom_button] double tap, nothing bound")

        elif tap == 'long':
            self._toggle_passive()

    def _toggle_passive(self):
        """
        Switch between AUTO and PASV. Deliberately inert in manual mode: a manual
        pwnagotchi transmits nothing to begin with, so there is nothing to switch
        off and the display would be claiming a state that means nothing there.
        """
        mode = getattr(self._agent, 'mode', None)
        if mode != 'auto':
            logging.info("[pisugar_custom_button] long press ignored, mode is %s", mode)
            return

        logging.info("[pisugar_custom_button] long press -> %s", self.event)
        try:
            plugins.on(self.event)
        except Exception as e:
            logging.error("[pisugar_custom_button] %s failed: %s", self.event, e)

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        self.poll_interval = max(1, int(self.options.get('poll_interval', 2)))
        self.event = str(self.options.get('event', 'pasv_toggle'))

        try:
            from smbus2 import SMBus
            self._bus = SMBus(I2C_BUS)
        except Exception as e:
            logging.error("[pisugar_custom_button] cannot open i2c bus %d: %s", I2C_BUS, e)
            return

        logging.info("[pisugar_custom_button] loaded, poll every %ds, long press sends %r",
                     self.poll_interval, self.event)

        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def on_ui_update(self, ui):
        # The agent carries the current mode, and on_ready never fires in manual
        # mode because that event comes from automata.py, which is only reached
        # through agent.start().
        if self._agent is None:
            self._agent = getattr(ui, '_agent', None)

    def on_ready(self, agent):
        self._agent = agent

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
