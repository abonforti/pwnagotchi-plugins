# pwnagotchi-plugins

[![checks](https://github.com/abonforti/pwnagotchi-plugins/actions/workflows/ci.yml/badge.svg)](https://github.com/abonforti/pwnagotchi-plugins/actions/workflows/ci.yml)

Plugins for [jayofelony/pwnagotchi](https://github.com/jayofelony/pwnagotchi), written for a
Raspberry Pi Zero 2 W with a Waveshare 2.13" v3 display, 250x122, and a PiSugar 3.

| Plugin | What it does |
| --- | --- |
| [`pisugar_power_button.py`](pisugar_power_button.py) | Long press on the PiSugar power button syncs and halts, then cuts power, instead of dropping the rail mid write |
| [`pisugarx_ext.py`](pisugarx_ext.py) | Fork of the bundled `pisugarx`: `BAT` / `CHG` / `EXT` instead of `CHG` at any charge, standalone voltage and temperature with an out of range marker, two upstream fixes |
| [`age_caps.py`](age_caps.py) | Fork of AlienMajik's `age`: uppercase labels, `EXP` instead of `Next Age`, three upstream fixes |
| [`memtemp_caps.py`](memtemp_caps.py) | Fork of the bundled `memtemp`: uppercase headers, heavier fonts |
| [`pisugar_custom_button.py`](pisugar_custom_button.py) | Long press on the PiSugar custom button switches between AUTO and PASV; single and double are reserved |
| [`pasv_mode.py`](pasv_mode.py) | A third state between AUTO and MANU: keeps listening, stops deauthenticating, associating and advertising |
| [`ui_tweaks.py`](ui_tweaks.py) | Moves and restyles built-in elements, and adds a connectivity indicator with the pwngrid inbox counter |

Each plugin documents its own options, its reasoning and its caveats in `__help__`, which is what
the CLI shows:

```
sudo pwnagotchi plugins list -i
```

## Installing

Add this repository to `custom_plugin_repos` in `/etc/pwnagotchi/config.toml`, keeping the entries
that are already there. The list replaces the default one rather than extending it, because
`merge_config()` only recurses into dictionaries:

```toml
custom_plugin_repos = [
    "https://github.com/jayofelony/pwnagotchi-torch-plugins/archive/master.zip",
    "https://github.com/Sniffleupagus/pwnagotchi_plugins/archive/master.zip",
    "https://github.com/NeonLightning/pwny/archive/master.zip",
    "https://github.com/marbasec/UPSLite_Plugin_1_3/archive/master.zip",
    "https://github.com/wpa-2/Pwnagotchi-Plugins/archive/master.zip",
    "https://github.com/cyberartemio/wardriver-pwnagotchi-plugin/archive/main.zip",
    "https://github.com/abonforti/pwnagotchi-plugins/archive/master.zip",
]
```

Then:

```
sudo pwnagotchi plugins update
sudo pwnagotchi plugins list
sudo pwnagotchi plugins install ui_tweaks
```

A section in `config.toml` is still required, and its name must match the file name exactly. The
loader compares strings and skips anything that does not match, without logging a thing, so a
typo produces a plugin that installs, sits there and never runs.

Where a plugin forks something bundled or third party, disable the original: both would load, both
would poll the same device and both would draw over each other.

| Fork | Disable |
| --- | --- |
| `pisugarx_ext` | `pisugarx` |
| `age_caps` | `age` |
| `memtemp_caps` | `memtemp` |

## Layout

Every plugin is a single `.py` file **in the repository root**. This is not a style choice.
`pwnagotchi plugins update` unpacks the archive with `strip_dirs=1`, which removes only the
wrapper directory GitHub adds, and `_get_available()` then scans one level deep:

```python
    for filename in glob.glob(os.path.join(SAVE_DIR, "*.py")):
```

A plugin in a subdirectory is invisible to `list` and `install`. CI enforces this.

Two consequences worth remembering:

- Only `.yml` and `.yaml` files sitting next to a plugin are copied along with it on install.
  Markdown next to a plugin is never installed, so per-plugin documentation belongs in `__help__`.
- Every configured repository unpacks into the same `available-plugins` directory, so filenames
  collide across repositories. Names here are prefixed by the hardware or the plugin they extend.

## Secrets

Nothing in this repository is a secret and nothing should become one. Plugins take API keys,
tokens and passwords from `config.toml` on the device, never from the source, and the `__help__`
blocks show placeholders only.

CI refuses a commit that looks like a leak: private key blocks, `sk_` prefixed keys, and anything
assigned to a variable named `api_key`, `token`, `secret` or `password` that is not obviously a
placeholder. It is a backstop for accidents, not a substitute for reading the diff.

Beyond keys, remember that a pwnagotchi configuration also carries the whitelist, which is a list
of the networks you own.

## Checks

```
python .github/check_plugins.py
```

Runs on every push and pull request. It parses rather than imports, so pwnagotchi does not need to
be installed and a pull request from a fork can never execute anything. It verifies that plugins
parse, that each declares one class deriving from `plugins.Plugin` with `__author__`,
`__version__`, `__license__` and `__description__`, that none has strayed into a subdirectory, and
that nothing resembling a credential has been committed. A missing `__help__` is reported as a
note rather than a failure.

## Licence

GPL-3.0, see [LICENSE](LICENSE). The forks keep the licence and the attribution of the code they
derive from: `age_caps.py` is MIT, from AlienMajik.
