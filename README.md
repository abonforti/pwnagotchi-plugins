# pwnagotchi-plugins

Plugins for [jayofelony/pwnagotchi](https://github.com/jayofelony/pwnagotchi), written for a
Raspberry Pi Zero 2 W with a Waveshare 2.13" v3 display and a PiSugar 3.

| Plugin | What it does |
| --- | --- |
| [`pisugar_power_button.py`](pisugar_power_button.py) | Turns the PiSugar 3 power button into a graceful shutdown button |
| [`memtemp_caps.py`](memtemp_caps.py) | Cosmetic fork of the bundled `memtemp`: uppercase headers, heavier fonts |
| [`age_caps.py`](age_caps.py) | Cosmetic fork of AlienMajik's `age`: uppercase labels, `EXP` instead of `Next Age` |
| [`ui_tweaks.py`](ui_tweaks.py) | Rewrites the prompt character after the name and drops the seconds from `UP` |

## Installing

Add this repository to `custom_plugin_repos` in `/etc/pwnagotchi/config.toml`:

```toml
custom_plugin_repos = [
    "https://github.com/abonforti/pwnagotchi-plugins/archive/master.zip",
]
```

Then:

```
sudo pwnagotchi plugins update
sudo pwnagotchi plugins list
sudo pwnagotchi plugins install pisugar_power_button
```

Each plugin documents its own options and caveats in `__help__`, which is what the CLI shows:

```
sudo pwnagotchi plugins list -i
```

## Layout

Every plugin is a single `.py` file **in the repository root**. This is not a style choice.
`pwnagotchi plugins update` unpacks the archive with `strip_dirs=1`, which removes only the
wrapper directory GitHub adds, and `_get_available()` then scans one level deep:

```python
    for filename in glob.glob(os.path.join(SAVE_DIR, "*.py")):
```

A plugin in a subdirectory is invisible to `list` and `install`.

Two consequences worth remembering:

- Only `.yml` and `.yaml` files sitting next to a plugin are copied along with it on install.
  Markdown next to a plugin is never installed, so per-plugin documentation belongs in `__help__`.
- Every configured repository unpacks into the same `available-plugins` directory, so filenames
  collide across repositories. Plugin names here are prefixed by the hardware they drive.
