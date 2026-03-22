#!/usr/bin/env python3
"""
Moteur de surveillance DAB+ — DAB+ Monitor
Pilote welle-cli via son API HTTP (/mux.json, /mp3/<SID>)
et surveille l'ensemble du bouquet + chaque service.

Architecture :
    welle-cli -c CHANNEL -Dw PORT
         ↓ HTTP poll /mux.json toutes les N secondes
    DABPlusMonitor
         ├── métriques RF globales (SNR, FIC quality, FFT offset...)
         ├── liste services dynamique
         ├── surveillance silence par service
         ├── alertes email
         └── streaming Icecast via /mp3/<SID>
"""

import subprocess
import threading
import queue
import json
import logging
import time
import os
import requests
from datetime import datetime
from email_alert import EmailAlert
from database import FMDatabase   # réutilisé tel quel

logger = logging.getLogger(__name__)

# ── Timeouts et constantes ──────────────────────────────────────────────────
WELLE_STARTUP_TIMEOUT = 15    # secondes pour que welle-cli soit prêt
POLL_RETRIES          = 3     # tentatives avant de déclarer welle-cli mort


class DABPlusMonitor:
    """
    Surveille un multiplex DAB+ complet via l'API HTTP de welle-cli.
    Un service peut être streamé en MP3 vers Icecast2.
    """

    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        self.ens_config  = self.config['ensemble']
        self.rtl_config  = self.config['rtl_sdr']
        self.welle_cfg   = self.config['welle_cli']
        self.mon_cfg     = self.config['monitoring']
        self.stream_cfg  = self.config['streaming']

        # welle-cli URL de base
        self.welle_port  = int(self.welle_cfg['port'])
        self.welle_url   = f"http://localhost:{self.welle_port}"
        self.poll_interval = float(self.welle_cfg.get('poll_interval', 2))

        # Processus / threads
        self.welle_process   = None
        self.stream_process  = None
        self.running         = False

        # ── État RF global ──────────────────────────────────────────────
        self.ensemble_ok    = False   # FIC décodé + SNR acceptable
        self.ensemble_lost  = False
        self.ensemble_alert_sent = False
        self.ensemble_lost_start = None

        # ── Services ────────────────────────────────────────────────────
        # dict SID (str) → ServiceState
        self.services = {}
        self.services_lock = threading.Lock()

        # ── Métriques RF ────────────────────────────────────────────────
        self.rf_metrics = {
            'snr':            0.0,
            'fic_quality':    0.0,
            'fic_errors':     0,
            'freq_corr':      0.0,
            'gain':           0.0,
            'ensemble_label': '',
            'ensemble_id':    '',
            'nb_services':    0,
        }
        self.rf_lock = threading.Lock()

        # ── Streaming audio ─────────────────────────────────────────────
        self.active_sid = self.stream_cfg.get('default_sid', '')
        self._stream_lock  = threading.Lock()
        self.stream_queue = queue.Queue(maxsize=500)

        # ── Alertes ─────────────────────────────────────────────────────
        self.email_alert = EmailAlert(config_path)
        self.db          = FMDatabase()
        self.last_db_save = time.time()

        # ── Stats exposées à l'API ──────────────────────────────────────
        self.stats_lock  = threading.Lock()

        # ── Historique uptime services (24h) ────────────────────────────
        self.uptime_stats = {}
        self.uptime_lock  = threading.Lock()

        # ── Historique alertes (24h glissantes) ─────────────────────────
        self.alert_history = []
        self.alert_lock    = threading.Lock()
        self.stats = {
            'start_time':       None,
            'uptime':           0,
            'alerts_sent':      0,
            'last_alert':       None,
            'status':           'Arrêté',
            'channel':          self.ens_config['channel'],
            'frequency':        self.ens_config['frequency_mhz'],
            'ensemble_label':   '',
            'ensemble_id':      '',
        }

    def _send_alert_tracked(self, alert_type, details, skip_cooldown=False):
        """Envoie une alerte email et l'enregistre dans l'historique 24h."""
        self.email_alert.send_alert(alert_type, details, skip_cooldown=skip_cooldown)
        now = time.time()
        entry = {'ts': now, 'type': alert_type, 'details': details}
        cutoff = now - 86400
        with self.alert_lock:
            self.alert_history.append(entry)
            # Purger > 24h
            self.alert_history = [a for a in self.alert_history if a['ts'] > cutoff]
        with self.stats_lock:
            self.stats['alerts_sent'] += 1
            self.stats['last_alert'] = datetime.fromtimestamp(now).strftime('%d/%m/%Y %H:%M')

    # ═══════════════════════════════════════════════════════════════════════
    # Démarrage / arrêt
    # ═══════════════════════════════════════════════════════════════════════

    def start(self):
        if self.running:
            logger.warning("Le moniteur est déjà en cours d'exécution")
            return

        self.running = True
        self.stats['start_time'] = datetime.now()
        self.stats['status']     = 'Démarrage'

        # Tuer les instances welle-cli orphelines
        os.system("pkill -9 welle-cli 2>/dev/null")
        time.sleep(1)

        # Lancer welle-cli
        t = threading.Thread(target=self._launch_welle_cli, daemon=True)
        t.start()

        # Attendre que l'API soit disponible
        if not self._wait_for_welle():
            logger.error("welle-cli ne répond pas au démarrage")
            self.stats['status'] = 'Erreur welle-cli'
            return

        # Thread de polling /mux.json
        poll_t = threading.Thread(target=self._poll_loop, daemon=True)
        poll_t.start()

        # Thread de surveillance des alertes
        watch_t = threading.Thread(target=self._watchdog_loop, daemon=True)
        watch_t.start()

        # Thread VU-mètre ffmpeg
        vu_t = threading.Thread(target=self._vu_meter_loop, daemon=True)
        vu_t.start()

        # Thread streaming si activé
        if self.stream_cfg.get('enabled') and self.active_sid:
            stream_t = threading.Thread(target=self._start_streaming, daemon=True)
            stream_t.start()

        self.stats['status'] = 'En cours'
        logger.info(f"DAB+ Monitor démarré — canal {self.ens_config['channel']} "
                    f"({self.ens_config['frequency_mhz']} MHz)")

    def stop(self):
        logger.info("Arrêt du moniteur DAB+")
        self.running = False

        if self.stream_process:
            try:
                self.stream_process.kill()
            except Exception:
                pass

        os.system("pkill -9 welle-cli 2>/dev/null")
        os.system("pkill -9 ffmpeg 2>/dev/null")

        with self.services_lock:
            self.services.clear()

        self.stats['status'] = 'Arrêté'

    # ═══════════════════════════════════════════════════════════════════════
    # Lancement welle-cli
    # ═══════════════════════════════════════════════════════════════════════

    def _launch_welle_cli(self):
        """Lance welle-cli en mode décodage complet + webserver."""
        channel     = self.ens_config['channel']
        gain        = int(self.rtl_config.get('gain', -1))
        carousel    = int(self.welle_cfg.get('carousel_size', 0))
        ppm         = int(self.rtl_config.get('ppm_error', 0))

        # -D : décode tous les services simultanément (Pi4 4Go le supporte)
        # -C N : mode carousel si -D trop gourmand
        decode_mode = f"-C {carousel}" if carousel > 0 else "-D"

        gain_arg = f"-g {gain}" if gain >= 0 else "-g -1"

        cmd = (
            f"welle-cli "
            f"-c {channel} "
            f"{gain_arg} "
            f"{decode_mode}w {self.welle_port}"
        )

        logger.info(f"Lancement : {cmd}")

        try:
            self.welle_process = subprocess.Popen(
                cmd,
                shell=True,
                executable='/bin/bash',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.welle_process.wait()
        except Exception as e:
            logger.error(f"Erreur welle-cli : {e}")
        finally:
            if self.running:
                logger.error("welle-cli s'est arrêté — relance dans 5s")
                time.sleep(5)
                if self.running:
                    threading.Thread(target=self._launch_welle_cli, daemon=True).start()

    def _wait_for_welle(self) -> bool:
        """Attend que /mux.json réponde."""
        deadline = time.time() + WELLE_STARTUP_TIMEOUT
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.welle_url}/mux.json", timeout=2)
                if r.status_code == 200:
                    logger.info("welle-cli prêt")
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    # ═══════════════════════════════════════════════════════════════════════
    # Polling /mux.json
    # ═══════════════════════════════════════════════════════════════════════

    def _poll_loop(self):
        """Polling régulier de /mux.json."""
        fail_count = 0
        while self.running:
            try:
                r = requests.get(
                    f"{self.welle_url}/mux.json",
                    timeout=3
                )
                if r.status_code == 200:
                    data = r.json()
                    self._parse_mux_json(data)
                    fail_count = 0
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.debug(f"Poll erreur ({fail_count}) : {e}")

            if fail_count >= POLL_RETRIES:
                logger.warning("welle-cli ne répond plus")
                with self.rf_lock:
                    self.rf_metrics['snr'] = 0.0
                    self.rf_metrics['fic_quality'] = 0.0
                fail_count = 0

            time.sleep(self.poll_interval)

    def _parse_mux_json(self, data: dict):
        """
        Parse le JSON retourné par welle-cli (structure réelle v2.4).

        Structure réelle :
        {
          "demodulator": {
            "snr": 19.0,
            "frequencycorrection": -88.0,
            "fic": { "numcrcerrors": 1267 }
          },
          "ensemble": {
            "id": "0xf030",
            "label": { "label": "LA ROCHE SUR YON", "shortlabel": "LRSY" }
          },
          "receiver": {
            "hardware": { "gain": 29.7, "name": "RTLSDRBlog, Blog V4" }
          },
          "services": [
            {
              "sid": "0xfa41",
              "label": { "label": "GRAFFITI", "shortlabel": "GRAFFITI" },
              "dls": { "label": "Artiste - Titre", "lastchange": 0 },
              "audiolevel": { "left": 31325, "right": 26379, "time": 1234 },
              "channels": 2,
              "components": [
                {
                  "ascty": "DAB+",
                  "primary": true,
                  "subchannel": {
                    "bitrate": 88,
                    "subchid": 0,
                    "protection": "EEP 3-A",
                    "languagestring": "French"
                  }
                }
              ]
            }
          ]
        }
        """

        # ── Métriques RF globales ─────────────────────────────────────
        demod        = data.get('demodulator', {})
        ens          = data.get('ensemble', {})
        ens_label    = ens.get('label', {}).get('label', '')
        ens_id       = ens.get('id', '')
        snr          = float(demod.get('snr', 0))
        freq_corr    = float(demod.get('frequencycorrection', 0))
        fic_errors   = int(demod.get('fic', {}).get('numcrcerrors', 0))
        gain         = float(data.get('receiver', {}).get('hardware', {}).get('gain', 0))
        services_raw = data.get('services', [])

        # FIC quality : delta d'erreurs CRC entre deux polls (numcrcerrors est cumulatif)
        prev_errors  = self.rf_metrics.get('fic_errors', 0)
        delta_errors = max(0, fic_errors - prev_errors) if fic_errors >= prev_errors else 0
        fic_quality  = max(0.0, 100.0 - min(delta_errors * 2.0, 100.0))

        with self.rf_lock:
            self.rf_metrics['snr']            = snr
            self.rf_metrics['fic_quality']    = fic_quality
            self.rf_metrics['fic_errors']     = fic_errors
            self.rf_metrics['freq_corr']      = freq_corr
            self.rf_metrics['gain']           = gain
            self.rf_metrics['ensemble_label'] = ens_label
            self.rf_metrics['ensemble_id']    = ens_id
            self.rf_metrics['nb_services']    = len(services_raw)

        with self.stats_lock:
            self.stats['ensemble_label'] = ens_label
            self.stats['ensemble_id']    = ens_id

        # ── État de l'ensemble ────────────────────────────────────────
        snr_ok = snr >= float(self.mon_cfg.get('snr_threshold', 5.0))
        fic_ok = fic_quality >= float(self.mon_cfg.get('fic_quality_threshold', 80))
        self.ensemble_ok = snr_ok and fic_ok

        # ── Services ──────────────────────────────────────────────────
        now = time.time()

        with self.services_lock:
            current_sids = set()

            for svc in services_raw:
                sid = str(svc.get('sid', ''))
                if not sid:
                    continue

                label = svc.get('label', {}).get('label', sid).strip()
                current_sids.add(sid)

                if sid not in self.services:
                    self.services[sid] = _ServiceState(sid, label)
                    logger.info(f"Nouveau service découvert : {label} ({sid})")
                    # Démarrer le VU-mètre sur le premier vrai service si aucun actif
                    if not self.active_sid and len(label) > 2 and label not in ('..', '.'):
                        self.active_sid = sid
                        logger.info(f"Service VU-mètre par défaut : {label}")
                    # Démarrer le VU-mètre sur le premier vrai service si aucun actif
                    if not self.active_sid and len(label) > 2 and label not in ('..', '.'):
                        self.active_sid = sid
                        logger.info(f"Service VU-mètre par défaut : {label}")

                state       = self.services[sid]
                state.label = label or sid
                state.dls   = svc.get('dls', {}).get('label', '').strip()

                # Composant primaire
                components = svc.get('components', [])
                primary    = next((c for c in components if c.get('primary')), None)
                if primary is None and components:
                    primary = components[0]

                if primary:
                    sub = primary.get('subchannel', {})
                    state.mode        = primary.get('ascty', 'DAB+')
                    state.bitrate     = int(sub.get('bitrate', 0))
                    state.prot_info   = sub.get('protection', '')
                    state.language    = sub.get('languagestring', '')
                    state.subchannel_id = int(sub.get('subchid', 0))

                # Audio levels : valeurs brutes 0-32768 → dBFS
                audio = svc.get('audiolevel', {})
                raw_l = int(audio.get('left',  0))
                raw_r = int(audio.get('right', 0))
                state.audio_l  = _raw_to_dbfs(raw_l)
                state.audio_r  = _raw_to_dbfs(raw_r)
                state.channels = int(svc.get('channels', 2))

                # Metadonnees complementaires
                state.pty        = svc.get('ptystring', '')
                state.mode_full  = svc.get('mode', '')
                state.samplerate = int(svc.get('samplerate', 0))
                state.url_mp3    = svc.get('url_mp3', f'/mp3/{sid}')

                # Compteurs d'erreurs par service
                err = svc.get('errorcounters', {})
                state.err_aac   = int(err.get('aacerrors',   0))
                state.err_frame = int(err.get('frameerrors', 0))
                state.err_rs    = int(err.get('rserrors',    0))

                # Slideshow MOT
                mot = svc.get('mot', {})
                state.mot_time = int(mot.get('time', 0))

                state.present   = True
                state.last_seen = now

                # Détection silence (ignorer services fantômes sans vrai label)
                real_service = label not in ('..', '.', '') and len(label) > 2
                threshold = float(self.mon_cfg.get('audio_silence_threshold', -60.0))
                audio_avg = (state.audio_l + state.audio_r) / 2.0
                if real_service and audio_avg <= threshold:
                    if state.silence_start is None:
                        state.silence_start = now
                else:
                    if state.silence_start is not None:
                        state.silence_start      = None
                        state.silence_alert_sent = False
                        if real_service:
                            logger.info(f"Audio rétabli : {label}")

            # Services absents
            for sid, state in self.services.items():
                if sid not in current_sids and state.present:
                    state.present    = False
                    state.lost_start = now
                    logger.warning(f"Service absent du bouquet : {state.label} ({sid})")

    # ═══════════════════════════════════════════════════════════════════════
    # Watchdog alertes
    # ═══════════════════════════════════════════════════════════════════════

    def _watchdog_loop(self):
        """Surveille les seuils et envoie les alertes."""
        while self.running:
            try:
                self._check_ensemble_alerts()
                self._check_service_alerts()
                self._update_uptime()
            except Exception as e:
                logger.error(f"Watchdog erreur : {e}")
            time.sleep(1)

    def _check_ensemble_alerts(self):
        now = time.time()

        if not self.ensemble_ok:
            if self.ensemble_lost_start is None:
                self.ensemble_lost_start = now
                logger.warning("Ensemble DAB+ dégradé")
            elif (now - self.ensemble_lost_start >= self.mon_cfg.get('service_loss_duration', 60)
                  and not self.ensemble_alert_sent):
                snr = self.rf_metrics['snr']
                fic = self.rf_metrics['fic_quality']
                logger.error(f"Ensemble perdu — SNR={snr:.1f} dB, FIC={fic:.0f}%")
                self._send_alert_tracked(
                    alert_type="Ensemble DAB+ hors ligne",
                    details=(
                        f"Canal : {self.ens_config['channel']} "
                        f"({self.ens_config['frequency_mhz']} MHz)\n"
                        f"SNR : {snr:.1f} dB (seuil : "
                        f"{self.mon_cfg.get('snr_threshold', 5.0)} dB)\n"
                        f"FIC Quality : {fic:.0f}% (seuil : "
                        f"{self.mon_cfg.get('fic_quality_threshold', 80)}%)"
                    ),
                    skip_cooldown=True
                )
                self.ensemble_alert_sent = True
                with self.stats_lock:
                    self.stats['alerts_sent'] += 1
                    self.stats['last_alert'] = datetime.now().isoformat()
        else:
            if self.ensemble_alert_sent:
                logger.info("Ensemble DAB+ rétabli")
                self._send_alert_tracked(
                    alert_type="Ensemble DAB+ rétabli",
                    details=f"Canal {self.ens_config['channel']} — réception normale.",
                    skip_cooldown=True
                )
                self.ensemble_alert_sent = False
            self.ensemble_lost_start = None

    def _check_service_alerts(self):
        now = time.time()
        silence_dur   = float(self.mon_cfg.get('audio_silence_duration', 30))
        service_loss  = float(self.mon_cfg.get('service_loss_duration',  60))

        with self.services_lock:
            for sid, state in self.services.items():

                # ── Service absent du bouquet ─────────────────────────
                if not state.present and state.lost_start:
                    absence = now - state.lost_start
                    if absence >= service_loss and not state.lost_alert_sent:
                        logger.warning(
                            f"Service absent depuis {absence:.0f}s : {state.label}"
                        )
                        self._send_alert_tracked(
                            alert_type=f"Service DAB+ absent : {state.label}",
                            details=(
                                f"Le service {state.label} ({sid}) n'est plus "
                                f"détecté dans le bouquet depuis {int(absence)}s.\n"
                                f"Canal : {self.ens_config['channel']}"
                            ),
                            skip_cooldown=True
                        )
                        state.lost_alert_sent = True
                        with self.stats_lock:
                            self.stats['alerts_sent'] += 1
                            self.stats['last_alert'] = datetime.now().isoformat()

                elif state.present and state.lost_alert_sent:
                    logger.info(f"Service rétabli : {state.label}")
                    self._send_alert_tracked(
                        alert_type=f"Service DAB+ rétabli : {state.label}",
                        details=f"Le service {state.label} ({sid}) est de nouveau présent.",
                        skip_cooldown=True
                    )
                    state.lost_alert_sent = False
                    state.lost_start      = None

                # ── Silence audio ─────────────────────────────────────
                if (state.present and state.silence_start
                        and not state.silence_alert_sent):
                    duration = now - state.silence_start
                    if duration >= silence_dur:
                        audio_avg = (state.audio_l + state.audio_r) / 2.0
                        logger.warning(
                            f"Silence {state.label} depuis {duration:.0f}s "
                            f"({audio_avg:.1f} dBFS)"
                        )
                        self._send_alert_tracked(
                            alert_type=f"Silence audio : {state.label}",
                            details=(
                                f"Le service {state.label} ({sid}) est silencieux "
                                f"depuis {int(duration)}s.\n"
                                f"Niveau : {audio_avg:.1f} dBFS "
                                f"(seuil : {self.mon_cfg.get('audio_silence_threshold', -60)} dBFS)"
                            ),
                            skip_cooldown=True
                        )
                        state.silence_alert_sent = True
                        with self.stats_lock:
                            self.stats['alerts_sent'] += 1
                            self.stats['last_alert'] = datetime.now().isoformat()

    def _update_uptime(self):
        if self.stats['start_time']:
            uptime = (datetime.now() - self.stats['start_time']).total_seconds()
            with self.stats_lock:
                self.stats['uptime'] = int(uptime)

        # Tracker la présence de chaque service (pour stats uptime 24h)
        now = time.time()
        cutoff = now - 86400  # 24h
        with self.services_lock:
            services_snap = dict(self.services)
        with self.uptime_lock:
            for sid, svc in services_snap.items():
                if sid not in self.uptime_stats:
                    self.uptime_stats[sid] = {
                        'label':      svc.label,
                        'checks':     0,
                        'present':    0,
                        'first_seen': now,
                    }
                self.uptime_stats[sid]['label']   = svc.label
                self.uptime_stats[sid]['checks']  += 1
                if svc.present:
                    self.uptime_stats[sid]['present'] += 1
            # Purger les services non vus depuis 24h (nettoyage mémoire)
            to_del = [s for s, v in self.uptime_stats.items()
                      if v['first_seen'] < cutoff and s not in services_snap]
            for s in to_del:
                del self.uptime_stats[s]

    # ═══════════════════════════════════════════════════════════════════════
    # Streaming Icecast
    # ═══════════════════════════════════════════════════════════════════════

    def _vu_meter_loop(self):
        """
        Analyse audio du flux MP3 actif via ffmpeg ebur128.
        Extrait les niveaux L/R (FTPK) toutes les 100ms.
        Format: FTPK: -3.3 -0.5 dBFS
        """
        import re
        self._vu_process = None
        pattern = re.compile(r'lavfi\.astats\.(\d+)\.RMS_level=([+-]?[\d.]+|(-?)inf)')

        while self.running:
            sid = self.active_sid
            if not sid:
                time.sleep(1)
                continue

            source = f"http://localhost:{self.welle_port}/mp3/{sid}"
            cmd = [
                "ffmpeg", "-reconnect", "1",
                "-i", source,
                "-af", "astats=metadata=1:reset=1,ametadata=print",
                "-f", "null", "-"
            ]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=1
                )
                self._vu_process = proc

                levels = {}
                for raw in proc.stderr:
                    if not self.running or self.active_sid != sid:
                        break
                    try:
                        line = raw.decode("utf-8", errors="ignore").strip()
                        m = pattern.search(line)
                        if m:
                            ch  = int(m.group(1))
                            val = m.group(2)
                            db  = -100.0 if "inf" in val else float(val)
                            levels[ch] = db
                            if 1 in levels and 2 in levels:
                                with self.services_lock:
                                    if sid in self.services:
                                        self.services[sid].vu_l = levels[1]
                                        self.services[sid].vu_r = levels[2]
                                levels = {}
                    except Exception:
                        pass

                proc.wait()

            except Exception as e:
                logger.debug(f"VU-mètre ffmpeg : {e}")

            if self.running:
                time.sleep(2)


    def _kill_all_streaming(self):
        """Tue TOUS les ffmpeg streaming vers Icecast sans exception."""
        import subprocess as sp, time
        sp.run(['pkill', '-9', '-f', 'icecast://'], capture_output=True)
        sp.run(['pkill', '-9', '-f', 'content_type audio/mpeg'], capture_output=True)
        self._stream_pids  = []
        self._stream_procs = []
        self.stream_process = None
        time.sleep(0.5)

    def _start_streaming(self):
        """
        Streame le service actif vers Icecast via ffmpeg.
        Source : /mp3/<SID> de welle-cli.
        """
        if not self.active_sid:
            return

        icecast_url  = self.stream_cfg['icecast_url']
        output_rate  = self.stream_cfg.get('output_rate', '44100')
        bitrate      = self.stream_cfg.get('bitrate', '128k')
        source_url   = f"{self.welle_url}/mp3/{self.active_sid}"

        cmd = [
            'ffmpeg', '-reconnect', '1', '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', source_url,
            '-codec:a', 'libmp3lame', '-b:a', str(bitrate),
            '-ar', str(output_rate), '-ac', '2',
            '-content_type', 'audio/mpeg', '-f', 'mp3', icecast_url
        ]

        logger.info(f"Streaming SID {self.active_sid} → Icecast")

        sid_at_start = self.active_sid
        while self.running and self.active_sid == sid_at_start:
            try:
                self.stream_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                if not hasattr(self, '_stream_pids'):
                    self._stream_pids = []
                self._stream_pids.append(self.stream_process.pid)
                self.stream_process.wait()
                if self.active_sid != sid_at_start:
                    break
            except Exception as e:
                logger.error(f"Erreur streaming : {e}")
            if self.running and self.active_sid == sid_at_start:
                time.sleep(3)

    def switch_service(self, sid: str):
        """Change le service streamé vers Icecast."""
        if not self._stream_lock.acquire(blocking=False):
            logger.debug("Switch déjà en cours, ignoré")
            return
        try:
            logger.info(f"Switch service → {sid}")
            self.active_sid = sid
            self._kill_all_streaming()
            if hasattr(self, '_vu_process') and self._vu_process:
                try:
                    self._vu_process.kill()
                except Exception:
                    pass
            if self.stream_cfg.get('enabled') and sid:
                t = threading.Thread(target=self._start_streaming, daemon=True)
                t.start()
        finally:
            self._stream_lock.release()
    # ═══════════════════════════════════════════════════════════════════════
    # Données exposées à l'API Flask
    # ═══════════════════════════════════════════════════════════════════════

    def get_uptime_stats(self) -> list:
        """Retourne les stats de présence des services (24h)."""
        with self.uptime_lock:
            result = []
            for sid, v in self.uptime_stats.items():
                pct = round(v['present'] / v['checks'] * 100, 1) if v['checks'] > 0 else 0
                result.append({
                    'sid':        sid,
                    'label':      v['label'],
                    'checks':     v['checks'],
                    'present':    v['present'],
                    'uptime_pct': pct,
                    'first_seen': v['first_seen'],
                })
            return sorted(result, key=lambda x: x['label'])

    def get_alert_history(self) -> list:
        """Retourne l'historique des alertes des 24 dernières heures."""
        cutoff = time.time() - 86400
        with self.alert_lock:
            return sorted(
                [a for a in self.alert_history if a['ts'] > cutoff],
                key=lambda x: x['ts'], reverse=True
            )

    def get_stats(self) -> dict:
        """Retourne l'état complet pour le SSE Flask."""
        with self.stats_lock:
            stats = self.stats.copy()

        with self.rf_lock:
            stats['rf'] = self.rf_metrics.copy()

        stats['ensemble_ok']   = self.ensemble_ok
        stats['active_sid']    = self.active_sid
        stats['channel']       = self.ens_config['channel']
        stats['frequency_mhz'] = self.ens_config['frequency_mhz']

        with self.services_lock:
            stats['services'] = {
                sid: state.to_dict()
                for sid, state in self.services.items()
            }

        if stats.get('start_time'):
            stats['start_time'] = stats['start_time'].strftime('%d/%m/%Y %H:%M:%S')

        return stats

    def get_services_list(self) -> list:
        """Liste triée des services pour l'API."""
        with self.services_lock:
            return sorted(
                [s.to_dict() for s in self.services.values()],
                key=lambda x: x['label']
            )

    def get_welle_url(self) -> str:
        return self.welle_url


def _raw_to_dbfs(raw: int) -> float:
    """
    Convertit un niveau audio brut welle-cli (0-32768) en dBFS.
    32768 = 0 dBFS (pleine échelle 16 bits)
    """
    if raw <= 0:
        return -100.0
    import math
    return round(20.0 * math.log10(raw / 32768.0), 1)


# ═══════════════════════════════════════════════════════════════════════════
# État d'un service DAB+
# ═══════════════════════════════════════════════════════════════════════════

class _ServiceState:
    """Représente l'état de surveillance d'un service DAB+."""

    def __init__(self, sid: str, label: str):
        self.sid          = sid
        self.label        = label
        self.pty          = ''
        self.language     = ''
        self.dls          = ''
        self.dls_time     = ''
        self.bitrate      = 0
        self.mode         = 'DAB+'
        self.audio_l      = -100.0
        self.audio_r      = -100.0
        self.subchannel_id = ''
        self.prot_info    = ''
        self.audio_srate  = 0
        self.mode_full    = ''
        self.samplerate   = 0
        self.pty          = ''
        self.url_mp3      = f'/mp3/{sid}'
        self.err_aac      = 0
        self.err_frame    = 0
        self.err_rs       = 0
        self.channels     = 2
        self.mot_time     = 0
        self.vu_l         = -100.0
        self.vu_r         = -100.0
        self.vu_l         = -100.0
        self.vu_r         = -100.0
        self.vu_l         = -100.0
        self.vu_r         = -100.0

        # Surveillance
        self.present         = True
        self.last_seen       = time.time()
        self.lost_start      = None
        self.lost_alert_sent = False
        self.silence_start   = None
        self.silence_alert_sent = False

    def to_dict(self) -> dict:
        return {
            'sid':            self.sid,
            'label':          self.label,
            'pty':            self.pty,
            'language':       self.language,
            'dls':            self.dls,
            'dls_time':       self.dls_time,
            'bitrate':        self.bitrate,
            'mode':           self.mode,
            'audio_l':        round(self.audio_l, 1),
            'audio_r':        round(self.audio_r, 1),
            'subchannel_id':  self.subchannel_id,
            'prot_info':      self.prot_info,
            'audio_srate':    self.audio_srate,
            'present':        self.present,
            'silence':        self.silence_start is not None,
            'silence_alert':  self.silence_alert_sent,
            'lost_alert':     self.lost_alert_sent,
            'mode_full':      self.mode_full,
            'samplerate':     self.samplerate,
            'pty':            self.pty,
            'url_mp3':        self.url_mp3,
            'channels':       self.channels,
            'err_aac':        self.err_aac,
            'err_frame':      self.err_frame,
            'err_rs':         self.err_rs,
            'mot_time':       self.mot_time,
            'vu_l':           round(self.vu_l, 1),
            'vu_r':           round(self.vu_r, 1),
        }
