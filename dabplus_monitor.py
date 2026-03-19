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
            'snr':         0.0,
            'fic_quality': 0.0,
            'fft_offset':  0,
            'freq_corr':   0,
            'ensemble_label': '',
            'ensemble_id':    '',
            'nb_services':    0,
        }
        self.rf_lock = threading.Lock()

        # ── Streaming audio ─────────────────────────────────────────────
        self.active_sid = self.stream_cfg.get('default_sid', '')
        self.stream_queue = queue.Queue(maxsize=500)

        # ── Alertes ─────────────────────────────────────────────────────
        self.email_alert = EmailAlert(config_path)
        self.db          = FMDatabase()
        self.last_db_save = time.time()

        # ── Stats exposées à l'API ──────────────────────────────────────
        self.stats_lock = threading.Lock()
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
            f"{decode_mode}w {self.welle_port} "
            f"--disable-coarse-corrector"
            # ppm : welle-cli utilise le driver rtl_sdr directement
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
        Parse le JSON retourné par welle-cli.

        Structure attendue (welle.io) :
        {
          "channel": "9A",
          "snr": 15.2,
          "fic_quality": 100,
          "services": [
            {
              "sid": "0x1234",
              "label": "Graffiti Radio",
              "pty": "Pop Music",
              "language": "French",
              "dls": "Artiste - Titre",
              "dls_time": "2026-03-18T...",
              "audio_level_left": -12.5,
              "audio_level_right": -12.3,
              "bitrate": 80,
              "mode": "DAB+",
              "subchannel_id": 2,
              "prot_info": "EEP-3A",
              "audio_srate": 48000,
              "audio_bitrate": 80
            },
            ...
          ]
        }
        """
        # ── Métriques RF globales ─────────────────────────────────────
        with self.rf_lock:
            self.rf_metrics['snr']            = float(data.get('snr', 0))
            self.rf_metrics['fic_quality']    = float(data.get('fic_quality', 0))
            self.rf_metrics['fft_offset']     = int(data.get('fft', {}).get('fft_correction', 0))
            self.rf_metrics['freq_corr']      = float(data.get('frequency_correction_coarse', 0))
            self.rf_metrics['ensemble_label'] = data.get('label', '')
            self.rf_metrics['ensemble_id']    = data.get('eid', '')
            self.rf_metrics['nb_services']    = len(data.get('services', []))

        # ── Mise à jour stats Flask ───────────────────────────────────
        with self.stats_lock:
            self.stats['ensemble_label'] = data.get('label', '')
            self.stats['ensemble_id']    = data.get('eid', '')

        # ── État de l'ensemble ────────────────────────────────────────
        fic_ok = self.rf_metrics['fic_quality'] >= self.mon_cfg.get('fic_quality_threshold', 80)
        snr_ok = self.rf_metrics['snr']         >= self.mon_cfg.get('snr_threshold', 5.0)
        self.ensemble_ok = fic_ok and snr_ok

        # ── Services ──────────────────────────────────────────────────
        services_raw = data.get('services', [])
        now = time.time()

        with self.services_lock:
            # Marquer les SIDs présents
            current_sids = set()

            for svc in services_raw:
                sid   = str(svc.get('sid', ''))
                label = svc.get('label', sid)

                if not sid:
                    continue

                current_sids.add(sid)

                # Créer l'état si nouveau service
                if sid not in self.services:
                    self.services[sid] = _ServiceState(sid, label)
                    logger.info(f"Nouveau service découvert : {label} ({sid})")

                state = self.services[sid]
                state.label    = label
                state.pty      = svc.get('pty', '')
                state.language = svc.get('language', '')
                state.dls      = svc.get('dls', '')
                state.dls_time = svc.get('dls_time', '')
                state.bitrate  = int(svc.get('bitrate', 0))
                state.mode     = svc.get('mode', 'DAB+')
                state.audio_l  = float(svc.get('audio_level_left',  -100))
                state.audio_r  = float(svc.get('audio_level_right', -100))
                state.subchannel_id = svc.get('subchannel_id', '')
                state.prot_info     = svc.get('prot_info', '')
                state.audio_srate   = svc.get('audio_srate', 0)
                state.present       = True
                state.last_seen     = now

                # Détection silence
                threshold = float(self.mon_cfg.get('audio_silence_threshold', -60.0))
                audio_avg = (state.audio_l + state.audio_r) / 2.0
                if audio_avg < threshold:
                    if state.silence_start is None:
                        state.silence_start = now
                else:
                    state.silence_start = None
                    if state.silence_alert_sent:
                        state.silence_alert_sent = False
                        logger.info(f"Audio rétabli : {label}")

            # Services qui n'apparaissent plus dans le JSON
            for sid, state in self.services.items():
                if sid not in current_sids:
                    if state.present:
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
                self.email_alert.send_alert(
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
                self.email_alert.send_alert(
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
                        self.email_alert.send_alert(
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
                    self.email_alert.send_alert(
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
                        self.email_alert.send_alert(
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

    # ═══════════════════════════════════════════════════════════════════════
    # Streaming Icecast
    # ═══════════════════════════════════════════════════════════════════════

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

        cmd = (
            f"ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            f"-i {source_url} "
            f"-codec:a libmp3lame -b:a {bitrate} -ar {output_rate} -ac 2 "
            f"-content_type audio/mpeg -f mp3 "
            f"{icecast_url} 2>/dev/null"
        )

        logger.info(f"Streaming SID {self.active_sid} → Icecast")

        while self.running:
            try:
                self.stream_process = subprocess.Popen(
                    cmd, shell=True, executable='/bin/bash'
                )
                self.stream_process.wait()
            except Exception as e:
                logger.error(f"Erreur streaming : {e}")
            if self.running:
                time.sleep(3)

    def switch_service(self, sid: str):
        """Change le service streamé vers Icecast."""
        logger.info(f"Switch service → {sid}")
        self.active_sid = sid

        # Tuer ffmpeg actuel
        if self.stream_process:
            try:
                self.stream_process.kill()
            except Exception:
                pass

        # Relancer avec le nouveau SID
        if self.stream_cfg.get('enabled') and sid:
            t = threading.Thread(target=self._start_streaming, daemon=True)
            t.start()

    # ═══════════════════════════════════════════════════════════════════════
    # Données exposées à l'API Flask
    # ═══════════════════════════════════════════════════════════════════════

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
        }
