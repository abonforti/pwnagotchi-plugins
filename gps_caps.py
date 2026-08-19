import json
import logging
import os
import socket
import threading
import time

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK


class GPSCaps(plugins.Plugin):
    __author__ = "abonforti"
    __version__ = "1.0.0"
    __license__ = "GPL3"
    __description__ = "Stato GPS compatto da gpsd, e coordinate accanto agli handshake."
    __help__ = """
    A one character GPS status next to the other indicators, read from gpsd, plus the coordinates
    saved beside every handshake.

    gpsd owns the serial line exclusively, so nothing else may open it: this plugin is a second TCP
    client on port 2947, and so are pwn-gps and cgps. The bundled `gps` plugin must stay disabled,
    or bettercap will fight gpsd for the same device.

    Configuration:

      [main.plugins.gps_caps]
      enabled = true
      host = "127.0.0.1"
      port = 2947
      vertical = true        # label and value on two rows, for a crowded display
      position = [129, 84]
      line_spacing = 12      # only used when vertical
      label = "GPS"
      show_sats = true       # append the satellite count
      stale_after = 10       # seconds without a TPV before the module counts as mute
      down_after = 20        # seconds without any gpsd message before gpsd counts as gone
      reconnect_delay = 5

    States, worst to best:

      -  gpsd unreachable on 2947
      X  gpsd answers but no TPV for stale_after seconds: mute module, wrong baud, busy UART
      A  valid sentences, no fix: acquiring, or no sky
      2  2D fix, three satellites: latitude and longitude, altitude assumed
      F  3D fix, four or more: altitude too, and better accuracy on the flat

    The satellite count is those seen (nSat) until there is a fix, those used (uSat) after.

    On gpsd: run it with `-b` (read only). Without it gpsd reconfigures a u-blox receiver into
    binary UBX mode and stops the NMEA sentences, and while the fix stays correct the SKY reports
    lose the satellite list entirely, so the count sits at zero forever.
    """

    # Stati mostrati sul display, dal peggiore al migliore. Sono tre cose
    # indipendenti compresse in un carattere: gpsd raggiungibile, modulo che
    # parla, fix risolto.
    #
    #   -  gpsd non risponde su 2947 (socket giu', o daemon morto)
    #   X  gpsd risponde ma non arriva un TPV da stale_after secondi: il
    #      modulo e' muto o la UART e' occupata da qualcun altro
    #   A  frasi valide, nessun fix: acquisizione in corso o niente cielo
    #   2  fix 2D, tre satelliti: lat e lon, quota ipotizzata
    #   F  fix 3D, quattro o piu': anche la quota, e piu' preciso in piano
    ST_DOWN = "-"
    ST_MUTE = "X"
    ST_ACQ = "A"
    ST_2D = "2"
    ST_3D = "F"

    def __init__(self):
        self.options = dict()
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._connected = False
        self._last_gpsd = 0.0
        self._last_tpv = 0.0
        self._mode = 0
        self._sats_used = 0
        self._sats_seen = 0
        self._hdop = None
        self._fix = None
        self._last_state = None

    # --- configurazione ----------------------------------------------------

    def _opt(self, key, default):
        value = self.options.get(key, default)
        return default if value is None else value

    def on_loaded(self):
        self._running = True
        self._thread = threading.Thread(target=self._pump_forever, daemon=True)
        self._thread.start()
        logging.info(
            "[gps_caps] avviato, gpsd su %s:%d"
            % (self._opt("host", "127.0.0.1"), int(self._opt("port", 2947)))
        )

    def on_unload(self, ui):
        self._running = False
        for element in ("gps_caps", "gps_caps_label"):
            try:
                ui.remove_element(element)
            except Exception:
                pass

    # --- lettura da gpsd ---------------------------------------------------

    def _pump_forever(self):
        # gpsd accetta piu' client contemporaneamente: qui siamo uno dei tanti,
        # nessuno tocca la seriale direttamente.
        while self._running:
            try:
                self._pump()
            except Exception as e:
                logging.debug("[gps_caps] connessione a gpsd caduta: %s" % e)
            with self._lock:
                self._connected = False
            time.sleep(float(self._opt("reconnect_delay", 5)))

    def _pump(self):
        host = self._opt("host", "127.0.0.1")
        port = int(self._opt("port", 2947))
        sock = socket.create_connection((host, port), timeout=10)
        try:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            with self._lock:
                self._connected = True
                self._last_gpsd = time.monotonic()
                # Non azzeriamo _last_tpv: se il modulo stava gia' parlando
                # una riconnessione a gpsd non deve far lampeggiare la X.
            stream = sock.makefile("r", encoding="utf-8", errors="ignore")
            for line in stream:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    report = json.loads(line)
                except ValueError:
                    continue
                self._consume(report)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _consume(self, report):
        kind = report.get("class")
        with self._lock:
            self._last_gpsd = time.monotonic()
        if kind == "TPV":
            with self._lock:
                self._last_tpv = time.monotonic()
                self._mode = int(report.get("mode", 0) or 0)
                if self._mode >= 2:
                    lat = report.get("lat")
                    lon = report.get("lon")
                    if lat is not None and lon is not None and (lat or lon):
                        self._fix = {
                            "lat": lat,
                            "lon": lon,
                            "alt": report.get("altMSL", report.get("alt")),
                            "time": report.get("time"),
                            "mode": self._mode,
                        }
        elif kind == "SKY":
            seen = report.get("nSat")
            used = report.get("uSat")
            sats = report.get("satellites")
            if isinstance(sats, list):
                if seen is None:
                    seen = len(sats)
                if used is None:
                    used = sum(1 for s in sats if s.get("used"))
            with self._lock:
                self._sats_seen = int(seen) if seen is not None else self._sats_seen
                self._sats_used = int(used) if used is not None else self._sats_used
                hdop = report.get("hdop")
                self._hdop = hdop if hdop else self._hdop

    # --- stato -------------------------------------------------------------

    def _snapshot(self):
        stale_after = float(self._opt("stale_after", 10))
        # Con il ricevitore senza corrente gpsd chiude il socket e il thread si
        # riconnette in continuazione. Senza isteresi lo stato sbatterebbe fra
        # X e - ogni pochi secondi, riempiendo il log e facendo lampeggiare il
        # display. gpsd si considera perso solo dopo down_after secondi senza
        # nessun messaggio, non al primo socket caduto.
        down_after = float(self._opt("down_after", 20))
        with self._lock:
            connected = self._connected or (
                self._last_gpsd and time.monotonic() - self._last_gpsd <= down_after
            )
            age = time.monotonic() - self._last_tpv if self._last_tpv else None
            mode = self._mode
            seen = self._sats_seen
            used = self._sats_used
            fix = dict(self._fix) if self._fix else None
            hdop = self._hdop

        if not connected:
            state = self.ST_DOWN
        elif age is None or age > stale_after:
            state = self.ST_MUTE
        elif mode >= 3:
            state = self.ST_3D
        elif mode == 2:
            state = self.ST_2D
        else:
            state = self.ST_ACQ

        sats = used if state in (self.ST_2D, self.ST_3D) else seen
        return state, sats, fix, hdop

    # --- display -----------------------------------------------------------

    def on_ui_setup(self, ui):
        position = self._opt("position", [129, 84])
        if isinstance(position, str):
            position = [int(p) for p in position.split(",")]
        x, y = int(position[0]), int(position[1])
        label = str(self._opt("label", "GPS"))

        if self._opt("vertical", False):
            # Su due righe: l'etichetta sopra, il valore sotto. Serve dove la
            # riga e' gia' piena in orizzontale ma restano due fasce libere,
            # come la colonna fra la barra EXP e il blocco memtemp.
            spacing = int(self._opt("line_spacing", 12))
            ui.add_element(
                "gps_caps_label",
                Text(value=label, position=(x, y), font=fonts.Bold, color=BLACK),
            )
            ui.add_element(
                "gps_caps",
                Text(value=self.ST_DOWN, position=(x, y + spacing),
                     font=fonts.Medium, color=BLACK),
            )
        else:
            ui.add_element(
                "gps_caps",
                LabeledValue(
                    color=BLACK,
                    label=label,
                    value=self.ST_DOWN,
                    position=(x, y),
                    label_font=fonts.Bold,
                    text_font=fonts.Medium,
                ),
            )

    def on_ui_update(self, ui):
        state, sats, _, _ = self._snapshot()
        if self._opt("show_sats", True) and state not in (self.ST_DOWN, self.ST_MUTE):
            text = "%s %02d" % (state, min(sats, 99))
        else:
            text = state
        ui.set("gps_caps", text)

        if state != self._last_state:
            # Un fix acquisito o perso vale una riga; l'andirivieni fra assente,
            # muto e in acquisizione no, o a ricevitore spento il log diventa
            # illeggibile.
            interesting = self.ST_2D in (state, self._last_state) or self.ST_3D in (
                state, self._last_state
            )
            level = logging.info if interesting else logging.debug
            level("[gps_caps] stato %s -> %s" % (self._last_state, state))
            self._last_state = state

    # --- handshake ---------------------------------------------------------

    def on_handshake(self, agent, filename, access_point, client_station):
        state, sats, fix, hdop = self._snapshot()
        if not fix or state not in (self.ST_2D, self.ST_3D):
            logging.debug("[gps_caps] handshake senza fix, nessun .gps.json")
            return

        base = filename
        for suffix in (".pcapng", ".pcap"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        gps_filename = base + ".gps.json"

        # Stesse chiavi che scrive il plugin gps stock (struct GPS di
        # bettercap): webgpsmap cerca Latitude, Longitude e Updated.
        payload = {
            "Updated": fix.get("time") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Latitude": fix["lat"],
            "Longitude": fix["lon"],
            "FixQuality": 1,
            "NumSatellites": sats,
            "HDOP": hdop if hdop else 0,
            "Altitude": fix.get("alt") if fix.get("alt") is not None else 0,
            "Separation": 0,
        }
        try:
            with open(gps_filename, "w+t") as fp:
                json.dump(payload, fp)
            logging.info("[gps_caps] salvato %s" % os.path.basename(gps_filename))
        except Exception as e:
            logging.error("[gps_caps] scrittura di %s fallita: %s" % (gps_filename, e))
