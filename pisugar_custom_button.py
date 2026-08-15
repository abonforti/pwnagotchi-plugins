import logging
import threading

import pwnagotchi
import pwnagotchi.plugins as plugins

# PiSugar 3 MCU
I2C_BUS = 1
I2C_ADDR = 0x57

REG_TAP = 0x08           # bits 0-1 latch the gesture until cleared
REG_WRITE_ENABLE = 0x0B  # 0x29 unlocks the other registers, anything else locks
REG_LED = 0xE0           # bits 0-3 drive the four green LEDs directly

WRITE_UNLOCK = 0x29
WRITE_LOCK = 0x00
TAP_MASK = 0b11
LED_MASK = 0b1111

# Same encoding pisugar-server uses in pisugar-core/src/pisugar3.rs
TAPS = {1: 'single', 2: 'double', 3: 'long'}

BUILTIN = ('none', 'leds', 'mode')


class PiSugarCustomButton(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = (
        'Binds the PiSugar 3 custom button to plugin events, to the board LEDs or to switching '
        'between AUTO and MANU.'
    )
    __help__ = """
    The custom button is the one on the edge nearest the Type-C port. Nothing reads it out of the
    box: the bundled pisugarx plugin has the tap getters as empty stubs, and the vendor's
    pisugar-server cannot be run alongside it because both would poll the same I2C device.

    The MCU latches the gesture in register 0x08 and keeps it there until it is cleared, so a two
    second poll never misses one. The encoding matches pisugar-core:

        1 => single, 2 => double, 3 => long

    Configuration:

      [main.plugins.pisugar_custom_button]
      enabled = true
      poll_interval = 2
      single = "pasv_toggle"
      double = "leds"
      long = "mode"

    Two of those are built in, because they act on this board and nothing else would know how:

      leds    toggles the four green LEDs through 0xE0. Useful when carrying the device around.
              The datasheet warns the control is not exclusive, so the register is reasserted on
              every poll while they are meant to stay off.
      mode    restarts the service through pwnagotchi.restart(), which leaves the override file
              that pwnlib's is_auto_mode() consumes on the next boot. That is what allows running
              in auto while the USB data cable is connected, since usb0 being up otherwise forces
              MANU. It restarts the service, so it is worth putting on the long press.

    Anything else is treated as the name of a plugin event and emitted as such:

        plugins.on('pasv_toggle')

    which reaches any plugin implementing on_pasv_toggle, without this one knowing what happens
    next. That is how the passive mode is driven: see pasv_mode.py, which also exposes the same
    switch through the web UI.

    Events are dispatched on the target plugin's own thread and return nothing, so a binding
    cannot report success here. Look at the log of whatever handles it.

    Runs alongside pisugar_power_button, which owns the power button. Both write to 0x0B to lift
    the write protection around a write, so in principle a tap and a shutdown landing in the same
    instant could interleave. Writes are rare enough on both sides that this has not been worth
    guarding against.
    """

    def __init__(self):
        self.options = dict()
        self.poll_interval = 2
        self.bindings = {}
        self._bus = None
        self._agent = None
        self._leds_off = False
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

    # --- actions -----------------------------------------------------------

    def _dispatch(self, tap):
        action = self.bindings.get(tap, 'none')
        if action == 'none':
            logging.debug("[pisugar_custom_button] %s tap, nothing bound", tap)
            return

        logging.info("[pisugar_custom_button] %s tap -> %s", tap, action)
        try:
            if action == 'leds':
                self._toggle_leds()
            elif action == 'mode':
                self._switch_mode()
            else:
                plugins.on(action)
        except Exception as e:
            logging.error("[pisugar_custom_button] %s failed: %s", action, e)

    def _toggle_leds(self):
        self._leds_off = not self._leds_off
        self._write_leds()
        logging.info("[pisugar_custom_button] LEDs %s",
                     "held off" if self._leds_off else "released")

    def _write_leds(self):
        led = self._read(REG_LED)
        self._write(REG_LED, (led & ~LED_MASK & 0xFF) if self._leds_off else (led | LED_MASK))

    def _switch_mode(self):
        current = getattr(self._agent, 'mode', None)
        wanted = 'MANU' if current == 'auto' else 'AUTO'
        logging.warning("[pisugar_custom_button] restarting in %s mode", wanted)
        pwnagotchi.restart(wanted)

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        self.poll_interval = max(1, int(self.options.get('poll_interval', 2)))

        for tap in TAPS.values():
            action = str(self.options.get(tap, 'none')).strip()
            self.bindings[tap] = action or 'none'

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

    def on_ready(self, agent):
        self._agent = agent

    def on_ui_update(self, ui):
        # Only needed by the mode action, and on_ready never fires in manual mode.
        if self._agent is None:
            self._agent = getattr(ui, '_agent', None)

    def on_unload(self, ui):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)

        # Give the LEDs back, otherwise unloading leaves a board that looks dead.
        if self._bus is not None:
            if self._leds_off:
                self._leds_off = False
                try:
                    self._write_leds()
                except OSError as e:
                    logging.warning("[pisugar_custom_button] cannot restore the LEDs: %s", e)
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
                elif self._leds_off:
                    self._write_leds()
            except OSError as e:
                # pisugarx polls the same device, so the odd collision is normal.
                logging.debug("[pisugar_custom_button] i2c failed: %s", e)

            self._stop.wait(self.poll_interval)
