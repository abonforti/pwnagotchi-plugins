# memtemp shows memory infos and cpu temperature
#
# mem usage, cpu load, cpu temp, cpu frequency
#
###############################################################
#
# Updated 18-10-2019 by spees <speeskonijn@gmail.com>
# - Changed the place where the data was displayed on screen
# - Made the data a bit more compact and easier to read
# - removed the label so we wont waste screen space
# - Updated version to 1.0.1
#
# 20-10-2019 by spees <speeskonijn@gmail.com>
# - Refactored to use the already existing functions
# - Now only shows memory usage in percentage
# - Added CPU load
# - Added horizontal and vertical orientation
#
# 19-09-2020 by crahan <crahan@n00.be>
# - Added CPU frequency
# - Made field types and order configurable (max 3 fields)
# - Made line spacing and position configurable
# - Updated code to dynamically generate UI elements
# - Changed horizontal UI elements to Text
# - Updated to version 1.0.2
#
# 15-08-2026 by abonforti
# - Forked from the memtemp plugin bundled with jayofelony/pwnagotchi as
#   memtemp_caps, so both can coexist and the original stays untouched
# - Uppercased the field headers
# - Bumped the fonts from Small to Medium, and the labels to Bold, to match
#   the weight of the CH/APS/UP/BT status bars on a 250x122 panel
###############################################################
from PIL import ImageFont
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK
import pwnagotchi.ui.fonts as fonts
import pwnagotchi.plugins as plugins
import pwnagotchi
import logging


class MemTemp(plugins.Plugin):
    __author__ = 'https://github.com/xenDE, spees, crahan, fork by abonforti'
    __version__ = '1.0.2-caps'
    __license__ = 'GPL3'
    __description__ = (
        'Memory, CPU usage and temperature, with uppercase headers and heavier fonts. Cosmetic '
        'fork of the bundled memtemp plugin, so the two can be installed side by side.'
    )
    __help__ = """
    Same plugin as the bundled memtemp, with a different look. It is a separate file rather than a
    patch because the change is a matter of taste, not a bug: upstream uses fonts.Small
    consistently in both orientations, which is coherent on its own. It only looks thin next to the
    CH/APS/UP/BT status bars, which are drawn in Bold and Medium.

    Differences from upstream:

      - field headers are uppercased, in both orientations
      - vertical labels use fonts.Bold, values use fonts.Medium
      - the horizontal header uses DejaVuSansMono-Bold at fonts.Medium size
      - the horizontal data row uses fonts.Medium

    Everything else, including the option names, is unchanged, so an existing memtemp
    configuration can be copied over verbatim:

      [main.plugins.memtemp_caps]
      enabled = true
      scale = "celsius"
      orientation = "horizontal"
      fields = "mem,cpu,temp"
      # linespacing = 10
      # position = "155,76"

    Disable the bundled memtemp when enabling this one. Nothing stops both from loading, and they
    would draw two overlapping blocks in the same place.

    Note: the horizontal header resolves DejaVuSansMono-Bold by name, without a path, so it needs
    fonts-dejavu present. That is the case on the pwnagotchi image.
    """

    ALLOWED_FIELDS = {
        'mem': 'mem_usage',
        'cpu': 'cpu_load',
        'cpus': 'cpu_load_since',
        'temp': 'cpu_temp',
        'freq': 'cpu_freq'
    }
    DEFAULT_FIELDS = ['mem', 'cpu', 'temp']
    LINE_SPACING = 10
    LABEL_SPACING = 0
    FIELD_WIDTH = 4
    def __init__(self):
        self.options = dict()

    def on_loaded(self):
        self._last_cpu_load = self._cpu_stat()
        logging.info("memtemp plugin loaded.")

    def mem_usage(self):
        return f"{int(pwnagotchi.mem_usage() * 100)}%"

    def cpu_load(self):
        return f"{int(pwnagotchi.cpu_load() * 100)}%"

    def _cpu_stat(self):
        """
        Returns the split first line of the /proc/stat file
        """
        with open('/proc/stat', 'rt') as fp:
            return list(map(int,fp.readline().split()[1:]))

    def cpu_load_since(self):
        """
        Returns the % load, since last time called
        """
        parts0 = self._cpu_stat()
        parts1 = self._last_cpu_load
        self._last_cpu_load = parts0

        parts_diff = [p1 - p0 for (p0, p1) in zip(parts0, parts1)]
        user, nice, sys, idle, iowait, irq, softirq, steal, _guest, _guest_nice = parts_diff
        idle_sum = idle + iowait
        non_idle_sum = user + nice + sys + irq + softirq + steal
        total = idle_sum + non_idle_sum
        return f"{int(non_idle_sum / total * 100)}%"

    def cpu_temp(self):
        if self.options['scale'] == "fahrenheit":
            temp = (pwnagotchi.temperature(celsius=False))
            symbol = "F"
        elif self.options['scale'] == "kelvin":
            temp = pwnagotchi.temperature() + 273.15
            symbol = "K"
        else:
            # default to celsius
            temp = pwnagotchi.temperature()
            symbol = "C"
        return f"{temp}{symbol}"

    def cpu_freq(self):
        with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 'rt') as fp:
            return f"{round(float(fp.readline())/1000000, 1)}G"

    def pad_text(self, data):
        return " " * (self.FIELD_WIDTH - len(data)) + data

    def on_ui_setup(self, ui):
        try:
            # Configure field list
            self.fields = self.options['fields'].split(',')
            self.fields = [x.strip() for x in self.fields if x.strip() in self.ALLOWED_FIELDS.keys()]
            self.fields = self.fields[:3]  # limit to the first 3 fields
        except Exception:
            # Set default value
            self.fields = self.DEFAULT_FIELDS

        try:
            # Configure line_spacing
            line_spacing = int(self.options['linespacing'])
        except Exception:
            # Set default value
            line_spacing = self.LINE_SPACING

        try:
            # Configure position
            pos = self.options['position'].split(',')
            pos = [int(x.strip()) for x in pos]
            if self.options['orientation'] == "vertical":
                v_pos = (pos[0], pos[1])
            else:
                h_pos = (pos[0], pos[1])
        except Exception:
            # Set default position based on screen type
            if ui.is_waveshare_v2():
                h_pos = (175, 84)
                v_pos = (197, 74)
            elif ui.is_waveshare_v1():
                h_pos = (170, 80)
                v_pos = (165, 61)
            elif ui.is_waveshare144lcd():
                h_pos = (53, 77)
                v_pos = (73, 67)
            elif ui.is_inky():
                h_pos = (140, 68)
                v_pos = (160, 54)
            elif ui.is_waveshare2in7():
                h_pos = (192, 138)
                v_pos = (211, 122)
            elif ui.is_waveshare1in54V2():
                h_pos = (53, 77)
                v_pos = (154, 65)
            else:
                h_pos = (155, 76)
                v_pos = (175, 61)

        if self.options['orientation'] == "vertical":
            # Dynamically create the required LabeledValue objects
            for idx, field in enumerate(self.fields):
                v_pos_x = v_pos[0]
                v_pos_y = v_pos[1] + ((len(self.fields) - 3) * -1 * line_spacing)
                ui.add_element(
                    f"memtemp_{field}",
                    LabeledValue(
                        color=BLACK,
                        label=f"{self.pad_text(field.upper())}:",
                        value="-",
                        position=(v_pos_x, v_pos_y + (idx * line_spacing)),
                        label_font=fonts.Bold,
                        text_font=fonts.Medium,
                        label_spacing=self.LABEL_SPACING,
                    )
                )
        else:
            # default to horizontal
            h_pos_x = h_pos[0] + ((len(self.fields) - 3) * -1 * 25)
            h_pos_y = h_pos[1]
            ui.add_element(
                'memtemp_header',
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text(x.upper()) for x in self.fields]),
                    position=(h_pos_x, h_pos_y),
                    font=ImageFont.truetype("DejaVuSansMono-Bold", fonts.Medium.size),
                )
            )
            ui.add_element(
                'memtemp_data',
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text("-") for x in self.fields]),
                    position=(h_pos_x, h_pos_y + line_spacing),
                    font=fonts.Medium,
                )
            )

    def on_unload(self, ui):
        with ui._lock:
            if self.options['orientation'] == "vertical":
                for idx, field in enumerate(self.fields):
                    ui.remove_element(f"memtemp_{field}")
            else:
                # default to horizontal
                ui.remove_element('memtemp_header')
                ui.remove_element('memtemp_data')

    def on_ui_update(self, ui):
        with ui._lock:
            if self.options['orientation'] == "vertical":
                for idx, field in enumerate(self.fields):
                    ui.set(f"memtemp_{field}", getattr(self, self.ALLOWED_FIELDS[field])())
            else:
                # default to horizontal
                data = " ".join([self.pad_text(getattr(self, self.ALLOWED_FIELDS[x])()) for x in self.fields])
                ui.set('memtemp_data', data)
