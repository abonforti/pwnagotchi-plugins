import logging
import threading
import time

import pwnagotchi
import pwnagotchi.plugins as plugins

# PiSugar 3 MCU
I2C_BUS = 1
I2C_ADDR = 0x57

REG_CTR1 = 0x02          # bit 5: output switch (cleared = delayed cut)
REG_CTR2 = 0x03          # bit 4: soft poweroff enable, bit 3: soft poweroff sign
REG_TAP = 0x08           # bits 0-1: 1 single, 2 double, 3 long, cleared by writing
REG_SHUTDOWN_DELAY = 0x09
REG_WRITE_ENABLE = 0x0B  # 0x29 unlocks the other registers, anything else locks
REG_LED = 0xE0           # bits 0-3 drive the four green LEDs directly

WRITE_UNLOCK = 0x29
WRITE_LOCK = 0x00

SOFT_POWEROFF_ENABLE = 1 << 4
SOFT_POWEROFF_SIGN = 1 << 3
OUTPUT_SWITCH = 1 << 5
TAP_MASK = 0b11
LED_MASK = 0b1111

TAPS = {1: 'single', 2: 'double', 3: 'long'}
ACTIONS = ('passive', 'leds', 'mode', 'none')


class PiSugarPowerButton(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.1.0'
    __license__ = 'GPL3'
    __description__ = (
        'Makes the PiSugar 3 buttons useful: the power button halts cleanly instead of dropping '
        'the rail mid write, and the custom button switches to passive mode, the LEDs or the '
        'AUTO/MANU mode.'
    )
    __help__ = """
    Out of the box, holding the PiSugar 3 power button for more than two seconds cuts the 5V rail
    immediately. With a mounted SD card that is a hard power loss at the worst possible moment.

    The MCU has a soft shutdown mode that replaces the direct cut with a flag, but nothing in
    pwnagotchi reads that flag: the bundled pisugarx plugin exposes the getters as empty stubs, and
    the vendor's pisugar-server cannot be used here because it would fight pisugarx for the same
    I2C device. This plugin closes that gap without a daemon.

    Behaviour:

      device off,     short press then press and hold  ->  powers on, unchanged
      device running, press and hold                   ->  syncs and halts, then power is cut
      device running, short press                      ->  nothing

    Configuration:

      [main.plugins.pisugar_power_button]
      enabled = true
      poll_interval = 2     # seconds between flag checks
      shutdown_delay = 45   # seconds before the PiSugar cuts power, 1-255

    On shutdown_delay: pwnagotchi.shutdown() sleeps a fixed ten seconds to refresh the display
    before it starts syncing, then rsyncs the zram mounts to the SD card, then hands over to halt,
    where systemd still has to stop bettercap, pwngrid and pwnagotchi and remount the filesystem
    read only. Twenty seconds leaves almost nothing for that tail. A longer delay costs a few tens
    of milliamperes for a few extra seconds on a 1200 mAh cell, so the default is generous on
    purpose.

    ## The custom button

    The MCU latches the gesture in register 0x08 and keeps it until it is cleared, so polling every
    couple of seconds never misses one. The encoding is the one pisugar-server uses: 1 single,
    2 double, 3 long. Nothing is bound by default.

      custom_single = "passive"
      custom_double = "leds"
      custom_long = "mode"
      passive_label = "PASV"

    passive stops provoking without stopping listening: recon and channel hopping continue and
    handshakes that happen anyway are still captured, but nothing is deauthenticated, associated
    or advertised. Meant for places where you would rather not transmit.

    The three flags do not behave alike. deauth and associate are read from the config on every
    call, so flipping them takes effect on the next one:

        if self._config['personality']['deauth'] and self._should_interact(sta['mac']):

    advertise is read once, in start_advertising(), so changing the flag afterwards does nothing:
    the thread is already running and pwngrid has already been told to advertise. It has to be
    turned off through the daemon, and grid.advertise(False) cannot do it because of an operator
    precedence bug in that function, so the endpoint is called directly.

    While passive, the mode element in the bottom right reads PASV instead of AUTO. That element
    is written from exactly one place in view.py, on_manual_mode, so in auto nothing else touches
    it. In manual it is left alone, since a manual pwnagotchi is not attacking anyway.

    leds toggles the four green LEDs through 0xE0. The datasheet warns the control is not
    exclusive, so the register is reasserted on every poll while they are meant to stay off.

    mode restarts the service through pwnagotchi.restart(), which leaves the override file that
    pwnlib's is_auto_mode() consumes on the next boot. That is what makes it possible to run in
    auto while the USB data cable is connected, since usb0 being up otherwise forces MANU.

    ## Verifying that passive really is passive

    The log line after a toggle reports what actually changed, including the HTTP status of the
    mesh call, rather than what was intended:

        [PiSugarPowerButton] passive=True deauth=False associate=False advertise call http 200

    Beyond that, the only honest test is from outside: put a second radio in monitor mode and look
    for beacons carrying the pwngrid vendor element, and for deauthentication frames from this
    unit's MAC.

    Trade-off: while this plugin is armed, holding the power button no longer cuts power by force.
    If the system hangs before the polling thread can react, the button does nothing. This is
    PiSugar issue 184, acknowledged by the vendor and still open. The escape hatch is the reset
    button on the PCB under the battery: a short press resets the MCU, which clears the soft
    shutdown bit and restores the direct cut. The plugin also disarms the mode in on_unload, so
    stopping pwnagotchi hands the button back to the hardware.
    """

    def __init__(self):
        self.options = dict()
        self.ready = False
        self._bus = None
        self._thread = None
        self._stop = threading.Event()
        self._shutting_down = False
        self.poll_interval = 2
        self.shutdown_delay = 45
        self.actions = {}
        self.passive_label = 'PASV'
        self.mesh_url = 'http://127.0.0.1:8666/api/v1/mesh/%s'
        self.mesh_timeout = 3
        self._agent = None
        self._passive = False
        self._leds_off = False

    # --- i2c helpers -------------------------------------------------------
    #
    # Every write is wrapped in the unlock/lock dance, mirroring what
    # pisugar-server does in pisugar-core/src/pisugar3.rs. Reads are never
    # write protected.

    def _read(self, reg):
        return self._bus.read_byte_data(I2C_ADDR, reg)

    def _write(self, reg, value):
        self._bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_UNLOCK)
        try:
            self._bus.write_byte_data(I2C_ADDR, reg, value)
        finally:
            self._bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_LOCK)

    # --- soft poweroff -----------------------------------------------------

    def _enable_soft_poweroff(self):
        """
        Arm the power button. Only bits 7-5 of CTR2 are preserved, the rest is
        rewritten from scratch: that is what the vendor implementation does, and
        bit 6 (automatic hibernate) is the only meaningful one in that range.
        """
        ctr2 = self._read(REG_CTR2)
        self._write(REG_CTR2, (ctr2 & 0b1110_0000) | SOFT_POWEROFF_ENABLE)

    def _soft_poweroff_armed(self):
        return bool(self._read(REG_CTR2) & SOFT_POWEROFF_ENABLE)

    def _soft_poweroff_requested(self):
        """
        The MCU only means "the user asked for a shutdown" when the enable bit
        and the sign bit are both set. Checking the sign bit alone gives false
        positives while the feature is off.
        """
        ctr2 = self._read(REG_CTR2)
        return (ctr2 & (SOFT_POWEROFF_ENABLE | SOFT_POWEROFF_SIGN)) == (
            SOFT_POWEROFF_ENABLE | SOFT_POWEROFF_SIGN
        )

    def _clear_soft_poweroff_sign(self):
        ctr2 = self._read(REG_CTR2)
        self._write(REG_CTR2, ctr2 & ~SOFT_POWEROFF_SIGN & 0xFF)

    def _arm_power_cut(self):
        """
        Tell the PiSugar to drop the 5V rail after shutdown_delay seconds. This
        has to be armed *before* handing control to pwnagotchi.shutdown(), which
        never returns, so the delay must outlast the whole shutdown sequence.
        """
        self._write(REG_SHUTDOWN_DELAY, self.shutdown_delay)
        ctr1 = self._read(REG_CTR1)
        self._write(REG_CTR1, ctr1 & ~OUTPUT_SWITCH & 0xFF)

    # --- custom button -----------------------------------------------------

    def _read_tap(self):
        """
        The MCU latches the gesture in 0x08 and keeps it until it is cleared, so a
        slow poll never misses one. Same encoding pisugar-server uses.
        """
        tap = self._read(REG_TAP) & TAP_MASK
        if tap:
            self._write(REG_TAP, self._read(REG_TAP) & ~TAP_MASK & 0xFF)
        return TAPS.get(tap)

    def _dispatch(self, tap):
        action = self.actions.get(tap, 'none')
        if action == 'none':
            logging.debug("[PiSugarPowerButton] %s tap, nothing bound", tap)
            return

        logging.info("[PiSugarPowerButton] %s tap -> %s", tap, action)
        try:
            getattr(self, '_action_%s' % action)()
        except Exception as e:
            logging.error("[PiSugarPowerButton] action %s failed: %s", action, e)

    def _action_passive(self):
        """
        Stop provoking without stopping listening. deauth and associate are read
        from the config on every call, so flipping them takes effect on the next
        one. advertise is not: it is only read once, in start_advertising(), so
        the mesh has to be told directly.
        """
        if self._agent is None:
            logging.warning("[PiSugarPowerButton] no agent yet, cannot switch to passive")
            return

        wanted = not self._passive
        personality = self._agent._config['personality']
        personality['deauth'] = not wanted
        personality['associate'] = not wanted

        mesh = 'off'
        try:
            import requests
            url = self.mesh_url % ('false' if wanted else 'true')
            response = requests.get(url, timeout=self.mesh_timeout)
            mesh = 'http %d' % response.status_code
        except Exception as e:
            # Not fatal: deauth and associate are already off, which is most of it.
            mesh = 'failed: %s' % e

        self._passive = wanted
        logging.warning(
            "[PiSugarPowerButton] passive=%s deauth=%s associate=%s advertise call %s",
            self._passive, personality['deauth'], personality['associate'], mesh,
        )

    def _action_leds(self):
        """
        0xE0 drives the four LEDs directly. The datasheet warns the control is not
        exclusive, so the register is rewritten on every poll while they are meant
        to stay off.
        """
        self._leds_off = not self._leds_off
        self._write_leds()
        logging.info("[PiSugarPowerButton] LEDs %s", "off" if self._leds_off else "released")

    def _write_leds(self):
        led = self._read(REG_LED)
        self._write(REG_LED, (led & ~LED_MASK & 0xFF) if self._leds_off else (led | LED_MASK))

    def _action_mode(self):
        """
        Restarts the service. pwnagotchi.restart() leaves the override file that
        pwnlib's is_auto_mode() consumes on the next boot, which is what makes this
        work while the USB data cable keeps forcing MANU.
        """
        current = getattr(self._agent, 'mode', None)
        wanted = 'MANU' if current == 'auto' else 'AUTO'
        logging.warning("[PiSugarPowerButton] restarting in %s mode", wanted)
        pwnagotchi.restart(wanted)

    # --- plugin lifecycle --------------------------------------------------

    def on_loaded(self):
        self.poll_interval = max(1, int(self.options.get('poll_interval', 2)))

        delay = int(self.options.get('shutdown_delay', 45))
        if not 1 <= delay <= 255:
            logging.warning(
                "[PiSugarPowerButton] shutdown_delay %d out of range 1-255, using 45", delay
            )
            delay = 45
        self.shutdown_delay = delay

        for tap in TAPS.values():
            action = str(self.options.get('custom_%s' % tap, 'none')).lower()
            if action not in ACTIONS:
                logging.warning("[PiSugarPowerButton] unknown action %r for the %s tap", action, tap)
                action = 'none'
            self.actions[tap] = action

        self.passive_label = str(self.options.get('passive_label', 'PASV'))[:4]
        self.mesh_url = self.options.get('mesh_url', self.mesh_url)
        self.mesh_timeout = max(1, int(self.options.get('mesh_timeout', 3)))

        try:
            from smbus2 import SMBus
            self._bus = SMBus(I2C_BUS)
        except Exception as e:
            logging.error("[PiSugarPowerButton] cannot open i2c bus %d: %s", I2C_BUS, e)
            return

        logging.info(
            "[PiSugarPowerButton] loaded, poll every %ds, power cut %ds after shutdown starts",
            self.poll_interval, self.shutdown_delay,
        )

        self._arm()

    def on_ready(self, agent):
        self._agent = agent
        # Arming already happened in on_loaded. This only covers being enabled at
        # runtime from the web config, where 'ready' is emitted right after
        # 'loaded' and the guard in _arm() makes the second call a no-op.
        self._arm()

    def _arm(self):
        """
        Arm the button and start polling. Deliberately not tied to on_ready:
        that event is emitted from automata.py, which is only reached through
        agent.start() in auto mode, so in manual mode it never fires at all.
        """
        if self._bus is None or self.ready:
            return

        try:
            self._enable_soft_poweroff()
        except OSError as e:
            logging.error("[PiSugarPowerButton] cannot arm the power button: %s", e)
            return

        self.ready = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        logging.info("[PiSugarPowerButton] power button armed for graceful shutdown")

    def on_ui_update(self, ui):
        # on_ready never fires in manual mode, and the agent is needed to reach
        # the personality settings, so pick it up from the view instead.
        if self._agent is None:
            self._agent = getattr(ui, '_agent', None)

        if 'mode' not in ui._state._state:
            return

        # view.py writes 'mode' from exactly one place, on_manual_mode, so in auto
        # nothing else touches it and there is no one to race with.
        if getattr(self._agent, 'mode', None) != 'auto':
            return
        ui.set('mode', self.passive_label if self._passive else 'AUTO')

    def on_unload(self, ui):
        self.ready = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 1)

        # Hand the button back to the hardware, otherwise a long press would do
        # nothing at all once nobody is polling the flag any more.
        if self._bus is not None and not self._shutting_down:
            try:
                ctr2 = self._read(REG_CTR2)
                self._write(REG_CTR2, ctr2 & ~SOFT_POWEROFF_ENABLE & 0xFF)
                logging.info("[PiSugarPowerButton] power button restored to hardware cut")
            except OSError as e:
                logging.warning("[PiSugarPowerButton] cannot restore the power button: %s", e)

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
                if self._soft_poweroff_requested():
                    self._on_button_pressed()
                    return
                # The MCU clears the enable bit whenever it restarts on its own,
                # so re-arm instead of silently losing the button.
                if not self._soft_poweroff_armed():
                    logging.info("[PiSugarPowerButton] soft poweroff was cleared, re-arming")
                    self._enable_soft_poweroff()

                tap = self._read_tap()
                if tap:
                    self._dispatch(tap)
                elif self._leds_off:
                    # The LED control is not exclusive, so it has to be reasserted.
                    self._write_leds()
            except OSError as e:
                # pisugarx polls the same device, so the odd collision is normal.
                logging.debug("[PiSugarPowerButton] i2c read failed: %s", e)

            self._stop.wait(self.poll_interval)

    def _on_button_pressed(self):
        if self._shutting_down:
            return
        self._shutting_down = True

        logging.warning("[PiSugarPowerButton] power button held, shutting down")

        try:
            self._clear_soft_poweroff_sign()
        except OSError as e:
            logging.warning("[PiSugarPowerButton] cannot clear the shutdown flag: %s", e)

        try:
            self._arm_power_cut()
            logging.warning(
                "[PiSugarPowerButton] PiSugar will cut power in %ds", self.shutdown_delay
            )
        except OSError as e:
            # Not fatal: the system still halts, the battery just keeps feeding
            # a halted board until it is drained or the button is held again.
            logging.error("[PiSugarPowerButton] cannot arm the power cut: %s", e)

        pwnagotchi.shutdown()
