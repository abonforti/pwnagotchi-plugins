import logging
import os
import threading

import pwnagotchi
import pwnagotchi.plugins as plugins

# pwngrid-peer listens here. Advertising cannot be stopped through the config,
# see the note in __help__.
MESH_URL = 'http://127.0.0.1:8666/api/v1/mesh/%s'


class PasvMode(plugins.Plugin):
    __author__ = 'abonforti'
    __version__ = '1.1.0'
    __license__ = 'GPL3'
    __description__ = (
        'A passive mode that keeps listening but stops transmitting: no deauthentication, no '
        'association, no mesh advertising. Controllable from the web UI, from a plugin event or '
        'from a webhook.'
    )
    __help__ = """
    Pwnagotchi has two states, AUTO and MANU, and nothing in between. This adds a third: recon and
    channel hopping carry on and handshakes that happen anyway are still captured, but the unit
    stops provoking and stops announcing itself. Meant for places where transmitting is a bad
    idea rather than places where you want the device off.

    The three settings involved do not behave alike, which is the whole reason this is a plugin
    rather than a line in a config file.

    deauth and associate are read from the config on every call, so flipping them in memory takes
    effect on the very next one:

        if self._config['personality']['deauth'] and self._should_interact(sta['mac']):

    advertise is read exactly once, in start_advertising(), which agent.start() calls at boot.
    Changing the flag afterwards does nothing at all: the polling thread is already running and
    pwngrid has already been told to advertise. It has to be turned off through the daemon.

    grid.advertise(False) cannot do it either. In that function % binds tighter than the
    conditional, so the false branch passes the bare string 'false' as the path and the request
    goes to /api/v1false. Only the true branch has ever worked. This plugin calls the endpoint
    directly and reports the HTTP status it got back.

    Configuration, all optional:

      [main.plugins.pasv_mode]
      enabled = true
      label = "PASV"      # shown in place of AUTO, four characters at most
      show_on_display = true
      mesh = true         # also stop advertising, not just attacking
      mesh_timeout = 3
      persist = true      # survive a restart, see below
      confd = "/etc/pwnagotchi/conf.d/"   # defaults to main.confd

    ## Controlling it

    Three ways in, all landing on the same code.

    From the web UI: the plugin appears at /plugins/pasv_mode, since anything defining on_webhook
    is listed there automatically. /plugins/pasv_mode/on, /off and /toggle act and redirect back,
    and /plugins/pasv_mode/status answers with JSON for scripting.

    From another plugin, without importing anything:

        import pwnagotchi.plugins as plugins
        plugins.on('pasv_toggle')          # or 'pasv_on' / 'pasv_off'

    Events are queued per plugin and dispatched on their own threads, so nothing is returned and
    the caller does not block. That is the point: a button plugin should not know how any of this
    works.

    ## Surviving a restart

    A unit that reboots in the place you went passive for, and comes back attacking, is worse than
    no passive mode at all. So the state persists.

    Not by rewriting config.toml, which save_config() would rewrite whole, dumping every merged
    default into a hand kept file. A drop-in is written to main.confd instead, holding nothing but
    the three flags. Drop-ins are merged after config.toml and take precedence:

        for conf in glob.glob(dropin):
            additional_config = load_toml_file(conf)
            config = merge_config(additional_config, config)

    So on the next boot the flags are false from the very first moment. That is not the same as
    restoring the state once running: start_advertising() is called during agent.start(), so a
    unit that restored afterwards would already have gone on the air. This way it never does.

    On load, the plugin only has to notice the file is there and adopt the state. Nothing to
    apply, since the config was already correct before anything started.

    The drop-in carries an empty [main] table, which is not decoration: load_toml_file() treats a
    file that does not contain that string as old style dotted toml, renames it to .ORIG and
    rewrites it.

    Leaving passive mode deletes the file. If it is ever left behind, deleting it by hand and
    restarting is enough, and the log says where it is at every load.

    ## What it does not do

    It cannot promise silence. It stops the transmissions pwnagotchi controls; it says
    nothing about the wlan0 interface itself, about bluetooth, or about anything else on the
    device. Verify from outside with a second radio in monitor mode: look for beacons carrying the
    pwngrid vendor element, and for deauthentication frames from this unit.
    """

    def __init__(self):
        self.options = dict()
        self.label = 'PASV'
        self.show_on_display = True
        self.mesh = True
        self.mesh_timeout = 3
        self.persist = True
        self.dropin = None
        self.passive = False
        self._agent = None
        self._lock = threading.Lock()

    # --- the actual switch -------------------------------------------------

    def set_passive(self, wanted):
        """
        Returns a dict describing what actually changed, not what was intended.
        """
        with self._lock:
            result = {'passive': self.passive, 'deauth': None, 'associate': None, 'mesh': 'skipped'}

            if self._agent is None:
                result['error'] = 'no agent yet'
                logging.warning("[pasv_mode] no agent yet, cannot switch")
                return result

            personality = self._agent._config['personality']
            personality['deauth'] = not wanted
            personality['associate'] = not wanted
            result['deauth'] = personality['deauth']
            result['associate'] = personality['associate']

            if self.mesh:
                result['mesh'] = self._set_mesh(not wanted)

            if self.persist:
                result['dropin'] = self._write_dropin(wanted)

            self.passive = wanted
            result['passive'] = wanted

            logging.warning(
                "[pasv_mode] passive=%s deauth=%s associate=%s mesh=%s dropin=%s",
                result['passive'], result['deauth'], result['associate'],
                result['mesh'], result.get('dropin', 'off'),
            )
            return result

    # --- surviving a restart -----------------------------------------------

    def _write_dropin(self, wanted):
        """
        Drop-ins in main.confd are merged after config.toml and win, so writing the
        three flags there makes them false from the very first moment of the next
        boot. Restoring the state after startup would not be equivalent:
        start_advertising() runs once during agent.start() and would already have
        put the unit on the air.
        """
        if not self.dropin:
            return 'no confd'

        try:
            if not wanted:
                if os.path.exists(self.dropin):
                    os.remove(self.dropin)
                return 'removed'

            os.makedirs(os.path.dirname(self.dropin), exist_ok=True)
            with open(self.dropin, 'w') as handle:
                handle.write(DROPIN)
            return 'written'
        except OSError as e:
            return 'failed: %s' % e

    def _set_mesh(self, enabled):
        try:
            import requests
            response = requests.get(MESH_URL % ('true' if enabled else 'false'),
                                    timeout=self.mesh_timeout)
            return 'http %d' % response.status_code
        except Exception as e:
            # Not fatal: deauth and associate are already off, which is most of it.
            return 'failed: %s' % e

    # --- events other plugins can send -------------------------------------

    def on_pasv_toggle(self):
        self.set_passive(not self.passive)

    def on_pasv_on(self):
        self.set_passive(True)

    def on_pasv_off(self):
        self.set_passive(False)

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        self.label = str(self.options.get('label', 'PASV'))[:4]
        self.show_on_display = bool(self.options.get('show_on_display', True))
        self.mesh = bool(self.options.get('mesh', True))
        self.mesh_timeout = max(1, int(self.options.get('mesh_timeout', 3)))
        self.persist = bool(self.options.get('persist', True))

        confd = self.options.get('confd')
        if not confd:
            try:
                confd = pwnagotchi.config['main']['confd']
            except (AttributeError, KeyError, TypeError):
                confd = None
        self.dropin = os.path.join(confd, 'pasv.toml') if confd else None

        # The drop-in was already merged at boot, so the flags are false and the
        # unit never went on the air. Nothing to apply: only the state to adopt.
        if self.persist and self.dropin and os.path.exists(self.dropin):
            self.passive = True
            logging.warning("[pasv_mode] started passive, %s is present", self.dropin)

        logging.info("[pasv_mode] loaded, label %r, mesh %s, dropin %s",
                     self.label, self.mesh, self.dropin or 'disabled')

    def on_ready(self, agent):
        self._agent = agent

    def on_ui_update(self, ui):
        # on_ready never fires in manual mode, since the event comes from
        # automata.py which is only reached through agent.start().
        if self._agent is None:
            self._agent = getattr(ui, '_agent', None)

        if not self.show_on_display or 'mode' not in ui._state._state:
            return

        # view.py writes 'mode' from exactly one place, on_manual_mode, so in auto
        # nothing else touches it and there is nobody to race with. In manual it
        # is left alone: a manual pwnagotchi is not transmitting anyway.
        if getattr(self._agent, 'mode', None) != 'auto':
            return
        ui.set('mode', self.label if self.passive else 'AUTO')

    # --- web ---------------------------------------------------------------

    def on_webhook(self, path, request):
        from flask import jsonify, redirect, render_template_string

        if path in ('on', 'off', 'toggle'):
            wanted = {'on': True, 'off': False}.get(path, not self.passive)
            self.set_passive(wanted)
            return redirect('/plugins/pasv_mode')

        if path == 'status':
            return jsonify({
                'passive': self.passive,
                'mesh': self.mesh,
                'label': self.label,
                'persist': self.persist,
                'dropin': self.dropin,
            })

        if path is None or path == '/':
            return render_template_string(PAGE, passive=self.passive, mesh=self.mesh,
                                          persist=self.persist)

        from flask import abort
        abort(404)


DROPIN = """# Written by the pasv_mode plugin. Delete this file to leave passive mode.
#
# Drop-ins in main.confd are merged after config.toml and take precedence, so
# these are in force from the first moment of the boot, before anything can
# transmit. The empty [main] table is not decoration: load_toml_file() treats a
# file without that string as old style dotted toml, renames it to .ORIG and
# rewrites it.
[main]

[personality]
advertise = false
deauth = false
associate = false
"""

PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Passive mode</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    .state { font-size: 2rem; font-weight: bold; margin: 1rem 0; }
    .on { color: #b00; }
    .off { color: #060; }
    a.btn { display: inline-block; padding: .6rem 1.2rem; border: 1px solid #333;
            text-decoration: none; color: #000; margin-right: .5rem; }
    p.note { color: #555; max-width: 40rem; }
  </style>
</head>
<body>
  <h1>Passive mode</h1>

  <div class="state {{ 'on' if passive else 'off' }}">
    {{ 'PASSIVE, not transmitting' if passive else 'ACTIVE, attacking normally' }}
  </div>

  <p>
    <a class="btn" href="/plugins/pasv_mode/toggle">Toggle</a>
    <a class="btn" href="/plugins/pasv_mode/status">Status as JSON</a>
  </p>

  <p class="note">
    Passive stops deauthentication and association{{ ' and mesh advertising' if mesh else '' }}.
    Recon and channel hopping continue, and handshakes that happen anyway are still captured.
  </p>
  <p class="note">
    {% if persist %}
    The state survives a restart: a drop-in in conf.d holds the three flags, so a reboot comes
    back passive without ever going on the air.
    {% else %}
    Persistence is off, so a restart brings back whatever config.toml says.
    {% endif %}
  </p>
</body>
</html>
"""
