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
REG_SHUTDOWN_DELAY = 0x09
REG_WRITE_ENABLE = 0x0B  # 0x29 unlocks the other registers, anything else locks

WRITE_UNLOCK = 0x29
WRITE_LOCK = 0x00

SOFT_POWEROFF_ENABLE = 1 << 4
SOFT_POWEROFF_SIGN = 1 << 3
OUTPUT_SWITCH = 1 << 5


class PiSugarPowerButton(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = (
        'Turns the PiSugar 3 power button into a graceful shutdown button. A long press no longer '
        'cuts power directly: it raises a flag that this plugin polls, so the filesystem is synced '
        'before the PiSugar removes power.'
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

    def on_ready(self, agent):
        if self._bus is None:
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

    def on_unload(self, ui):
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
