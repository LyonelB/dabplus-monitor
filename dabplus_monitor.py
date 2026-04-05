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
from database import DABDatabase

logger = logging.getLogger(__name__)

# ── Timeouts et constantes ──────────────────────────────────────────────────
WELLE_STARTUP_TIMEOUT = 15    # secondes pour que welle-cli soit prêt
POLL_RETRIES          = 3     # tentatives avant de déclarer welle-cli mort
STARTUP_GRACE_PERIOD  = 180  # secondes après démarrage : pas d'alertes silence/absence


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
            'snr':                  0.0,
            'fic_quality':          0.0,
            'fic_errors':           0,
            'freq_corr':            0.0,
            'gain':                 0.0,
            'ensemble_label':       '',
            'ensemble_id':          '',
            'nb_services':          0,
            'current_carousel_sid': '',  # welle-monitor fork
        }
        self.rf_lock = threading.Lock()

        # ── Streaming audio ─────────────────────────────────────────────
        self.active_sid = self.stream_cfg.get('default_sid', '')
        self._stream_lock  = threading.Lock()
        self.stream_queue = queue.Queue(maxsize=500)

        # ── Alertes ─────────────────────────────────────────────────────
        self.email_alert = EmailAlert(config_path)
        self.db          = DABDatabase('dab_monitor.db')
        self.last_db_save = time.time()

        # ── Moyenne glissante RF (15 secondes) ──────────────────────────
        from collections import deque as _deque
        self._rf_window   = 15   # secondes
        self._rf_buffers  = {
            'snr':         _deque(),
            'fic_quality': _deque(),
            'gain':        _deque(),
        }
        self._rf_buf_lock = threading.Lock()
        self._relaunch_lock = threading.Lock()  # empêche les relances simultanées
        self._launch_lock   = threading.Lock()  # empêche les lancements welle-cli simultanés
        self._is_restarting = False               # flag : une relance est en cours

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
        """Envoie une alerte email et l'enregistre dans l'historique 24h + base de données."""
        email_sent = self.email_alert.send_alert(alert_type, details, skip_cooldown=skip_cooldown)
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
        # Persistance en base de données
        try:
            self.db.save_alert(
                alert_type=alert_type,
                level_db=None,
                duration_seconds=None,
                message=details,
                email_sent=bool(email_sent)
            )
        except Exception as e:
            logger.debug(f"save_alert DB : {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # Démarrage / arrêt
    # ═══════════════════════════════════════════════════════════════════════

    def start(self):
        if self.running:
            logger.warning("Le moniteur est déjà en cours d'exécution")
            return

        self.running = True
        self.stats['start_time'] = datetime.now()

        # Charger les stats uptime depuis la base de données
        channel = self.ens_config.get('channel', '')
        cached_uptime = self.db.load_uptime(channel)
        if cached_uptime:
            with self.uptime_lock:
                self.uptime_stats.update(cached_uptime)
            logger.info(f"Uptime chargé depuis DB : {len(cached_uptime)} service(s)")

        # Charger les services connus depuis la base de données
        cached = self.db.load_services(channel)
        if cached:
            with self.services_lock:
                for svc in cached:
                    sid = svc['sid']
                    state = _ServiceState(sid, svc.get('label', sid))
                    state.bitrate      = svc.get('bitrate', 0)
                    state.prot_info    = svc.get('prot_info', '')
                    state.subchannel_id = svc.get('subchannel_id', 0)
                    state.language     = svc.get('language', '')
                    state.mode         = svc.get('mode', 'DAB+')
                    state.url_mp3      = svc.get('url_mp3', f'/mp3/{sid}')
                    state.enabled      = bool(svc.get('enabled', 1))
                    state.present      = False  # sera confirmé par le premier poll
                    self.services[sid] = state
            logger.info(f"{len(cached)} service(s) chargé(s) depuis le cache DB (canal {channel})")
        self.stats['status']     = 'Démarrage'

        # Tuer les instances welle-cli orphelines
        os.system("pkill -9 welle-cli 2>/dev/null")
        time.sleep(1)

        # Lancer welle-cli
        t = threading.Thread(target=self._launch_welle_cli, daemon=True)
        t.start()

        # Attendre que l'API soit disponible (non bloquant : on continue même si absent)
        if not self._wait_for_welle():
            logger.warning("welle-cli pas encore prêt — les threads démarrent quand même, "
                           "le watchdog prendra le relais dès que le signal sera présent")
            self.stats['status'] = 'En attente signal'
        else:
            self.stats['status'] = 'En cours'

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
        """Lance welle-cli en mode décodage complet + webserver.
        Le verrou _launch_lock garantit qu'une seule instance tourne à la fois.
        Compteur de crashs rapides : si welle-cli crashe 3 fois en < 5s,
        on attend 15s et on tente un reset USB du dongle.
        """
        # Empêcher les lancements simultanés (watchdog + auto-relance)
        if not self._launch_lock.acquire(blocking=True, timeout=10):
            logger.warning("_launch_welle_cli : verrou non acquis, abandon")
            return

        launch_time = time.time()
        lock_released = False

        try:
            # Tuer toute instance welle-cli existante avant d'en lancer une nouvelle
            if self.welle_process:
                try:
                    self.welle_process.kill()
                    self.welle_process.wait(timeout=3)
                except Exception:
                    pass
                self.welle_process = None
            os.system("pkill -9 welle-cli 2>/dev/null")
            time.sleep(1)  # laisser le dongle USB se libérer

            channel  = self.ens_config['channel']
            gain     = int(self.rtl_config.get('gain', -1))
            carousel = int(self.welle_cfg.get('carousel_size', 0))

            decode_mode = f"-C {carousel}" if carousel > 0 else "-D"
            gain_arg    = f"-g {gain}" if gain >= 0 else "-g -1"

            cmd = (
                f"welle-cli "
                f"-c {channel} "
                f"{gain_arg} "
                f"{decode_mode} "
                f"-w {self.welle_port}"
            )

            logger.info(f"Lancement : {cmd}")

            self.welle_process = subprocess.Popen(
                cmd,
                shell=True,
                executable='/bin/bash',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Libérer le lock dès que welle-cli est lancé
            self._launch_lock.release()
            lock_released = True

            self.welle_process.wait()

        except Exception as e:
            logger.error(f"_launch_welle_cli exception : {e}")
        finally:
            if not lock_released:
                try:
                    self._launch_lock.release()
                except RuntimeError:
                    pass

        if not self.running:
            return

        # ── Circuit breaker ─────────────────────────────────────────────────
        # Si welle-cli crashe trop vite, le dongle est probablement dans un
        # état incohérent. On arrête tout pendant CIRCUIT_OPEN_DELAY secondes
        # puis on réessaie une seule fois. Pas de boucle agressive.
        CIRCUIT_OPEN_DELAY = 300   # 5 minutes d'attente quand le dongle est mort
        FAST_CRASH_THRESHOLD = 5   # crashs rapides avant d'ouvrir le circuit
        FAST_CRASH_DELAY = 15      # délai entre chaque crash rapide

        crash_duration = time.time() - launch_time

        if crash_duration < 3:
            # Crash rapide : incrémenter le compteur (protégé par le launch_lock
            # qui est déjà libéré, mais _fast_crash_count est sur self)
            with threading.Lock():
                self._fast_crash_count = getattr(self, '_fast_crash_count', 0) + 1

            logger.warning(f"welle-cli crash rapide ({crash_duration:.1f}s) — "
                           f"compteur : {self._fast_crash_count}")

            if self._fast_crash_count >= FAST_CRASH_THRESHOLD:
                # Circuit ouvert : le dongle est mort
                logger.error(
                    f"Circuit breaker : {self._fast_crash_count} crashs rapides — "
                    f"pause {CIRCUIT_OPEN_DELAY}s avant prochaine tentative"
                )
                # Reset USB du dongle via sysfs
                os.system('for dev in /sys/bus/usb/devices/*/product; do '
                          'grep -q RTL "$dev" 2>/dev/null && '
                          'echo 0 > $(dirname $dev)/authorized && sleep 2 && '
                          'echo 1 > $(dirname $dev)/authorized; '
                          'done 2>/dev/null || true')

                self._fast_crash_count = 0

                # Acquérir le lock PENDANT toute la pause pour bloquer
                # tout autre thread qui tenterait de lancer welle-cli
                acquired = self._launch_lock.acquire(blocking=True, timeout=5)
                try:
                    for _ in range(CIRCUIT_OPEN_DELAY):
                        if not self.running:
                            return
                        time.sleep(1)
                finally:
                    if acquired:
                        try:
                            self._launch_lock.release()
                        except RuntimeError:
                            pass

                self._is_restarting = False
                # Relancer une seule fois après la pause
                if self.running:
                    logger.info("Circuit breaker : tentative de relance après pause")
                    threading.Thread(target=self._launch_welle_cli, daemon=True).start()
                return
            else:
                # Crash rapide mais circuit pas encore ouvert
                time.sleep(FAST_CRASH_DELAY)
        else:
            # Crash normal : welle-cli a vécu > 3s → réinitialiser le compteur
            self._fast_crash_count = 0
            logger.error("welle-cli s'est arrêté — relance dans 5s")
            time.sleep(5)

        self._is_restarting = False

        if self.running:
            threading.Thread(target=self._launch_welle_cli, daemon=True).start()

    def _wait_for_welle(self) -> bool:
        """Attend que welle-cli soit pret.
        Utilise /status (endpoint leger, ne freeze jamais) si disponible,
        sinon fallback sur /mux.json pour compatibilite avec welle-cli standard.
        """
        deadline = time.time() + WELLE_STARTUP_TIMEOUT
        while time.time() < deadline:
            try:
                # Essayer d'abord /status (welle-monitor fork)
                result = subprocess.run(
                    ['curl', '-s', '--max-time', '2', '--no-keepalive',
                     '-H', 'Connection: close',
                     '-o', '/dev/null', '-w', '%{http_code}',
                     f"{self.welle_url}/status"],
                    capture_output=True, text=True, timeout=4
                )
                if result.returncode == 0 and result.stdout.strip() == '200':
                    logger.info("welle-cli prêt (via /status)")
                    return True
                # Fallback /mux.json (welle-cli standard)
                result = subprocess.run(
                    ['curl', '-s', '--max-time', '2', '--no-keepalive',
                     '-H', 'Connection: close',
                     '-o', '/dev/null', '-w', '%{http_code}',
                     f"{self.welle_url}/mux.json"],
                    capture_output=True, text=True, timeout=4
                )
                if result.returncode == 0 and result.stdout.strip() == '200':
                    logger.info("welle-cli prêt (via /mux.json)")
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
        HARD_RESTART_AFTER = 5  # relancer welle-cli après N échecs consécutifs
        while self.running:
            try:
                r = requests.get(
                    f"{self.welle_url}/mux.json",
                    timeout=(3, 3),   # (connect_timeout, read_timeout) — hard limit sur les deux
                    headers={"Connection": "close"}
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

            # Après HARD_RESTART_AFTER échecs → relance welle-cli sans attendre le watchdog
            # Ne lancer qu'un seul watchdog à la fois grâce au flag _is_restarting
            if fail_count >= HARD_RESTART_AFTER and not self._is_restarting:
                logger.warning(f"Poll : {fail_count} échecs consécutifs — relance welle-cli")
                fail_count = POLL_RETRIES  # garder un niveau d'alerte sans respammer
                self._is_restarting = True
                threading.Thread(target=self._watchdog_system, daemon=True).start()

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

        # ── Champs welle-monitor fork ──────────────────────────────────
        # server_time : timestamp Unix côté serveur welle-cli
        # Permet de détecter si les données sont périmées (ex: signal coupé)
        server_time = data.get('server_time', None)
        if server_time:
            data_age = abs(time.time() - server_time)
            if data_age > 30:
                logger.debug(f"Données mux.json anciennes de {data_age:.0f}s")

        # current_carousel_sid : service actif en mode carousel (-C N)
        # Permet de savoir quel service est en cours de décodage
        current_carousel_sid = data.get('current_carousel_sid', None)
        if current_carousel_sid:
            with self.rf_lock:
                self.rf_metrics['current_carousel_sid'] = current_carousel_sid

        # FIC quality : delta d'erreurs CRC entre deux polls (numcrcerrors est cumulatif)
        prev_errors  = self.rf_metrics.get('fic_errors', 0)
        delta_errors = max(0, fic_errors - prev_errors) if fic_errors >= prev_errors else 0
        fic_quality  = max(0.0, 100.0 - min(delta_errors * 2.0, 100.0))

        # ── Moyenne glissante SNR / FIC / Gain (fenêtre 15s) ────────────
        now_ts = time.time()
        cutoff  = now_ts - self._rf_window
        with self._rf_buf_lock:
            for key, val in [('snr', snr), ('fic_quality', fic_quality), ('gain', gain)]:
                buf = self._rf_buffers[key]
                buf.append((now_ts, val))
                while buf and buf[0][0] < cutoff:
                    buf.popleft()
            snr_avg = sum(v for _, v in self._rf_buffers['snr'])         / len(self._rf_buffers['snr'])
            fic_avg = sum(v for _, v in self._rf_buffers['fic_quality']) / len(self._rf_buffers['fic_quality'])
            gain_avg= sum(v for _, v in self._rf_buffers['gain'])        / len(self._rf_buffers['gain'])

        with self.rf_lock:
            self.rf_metrics['snr']            = round(snr_avg, 1)
            self.rf_metrics['fic_quality']    = round(fic_avg, 1)
            self.rf_metrics['fic_errors']     = fic_errors
            self.rf_metrics['freq_corr']      = freq_corr
            self.rf_metrics['gain']           = round(gain_avg, 1)
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
                    # Sauvegarder en DB pour les prochains démarrages
                    try:
                        self.db.save_service(
                            sid=sid,
                            channel=self.ens_config.get('channel', ''),
                            label=label,
                            bitrate=int(svc.get('bitrate', 0) or 0),
                            prot_info=str(svc.get('prot_info', '') or ''),
                            language=str(svc.get('language', '') or ''),
                            mode=str(svc.get('mode', 'DAB+') or 'DAB+'),
                            url_mp3=str(svc.get('url_mp3', f'/mp3/{sid}') or f'/mp3/{sid}'),
                        )
                    except Exception:
                        pass
                else:
                    # Mettre à jour le label si changé
                    if self.services[sid].label != label and label not in ('..', '.', ''):
                        self.services[sid].label = label
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
                state.audio_time = int(audio.get('time', 0))
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
                        # Envoyer le mail de rétablissement si une alerte avait été envoyée
                        if real_service and state.silence_alert_sent:
                            logger.info(f"Audio rétabli : {label}")
                            self._send_alert_tracked(
                                alert_type=f"Audio rétabli : {label}",
                                details=(
                                    f"Le service {label} ({sid}) a retrouve un niveau audio normal.\n"
                                    f"Canal : {self.ens_config['channel']}"
                                ),
                                skip_cooldown=True
                            )
                            with self.stats_lock:
                                self.stats['alerts_sent'] += 1
                                self.stats['last_alert'] = datetime.now().isoformat()
                        elif real_service:
                            logger.info(f"Audio rétabli : {label}")
                        state.silence_start      = None
                        state.silence_alert_sent = False

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
        _sys_check_interval = 10   # vérifier welle-cli toutes les 10s
        _last_sys_check = 0.0
        while self.running:
            try:
                self._check_ensemble_alerts()
                self._check_service_alerts()
                self._update_uptime()
                # Watchdog system espacé pour ne pas surcharger welle-cli
                now = time.time()
                if now - _last_sys_check >= _sys_check_interval:
                    # Dans un thread pour ne pas bloquer les alertes/uptime
                    threading.Thread(target=self._watchdog_system, daemon=True).start()
                    _last_sys_check = now
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

        # Période de grâce au démarrage : pas d'alertes pendant STARTUP_GRACE_PERIOD s
        # Evite les fausses alertes de silence quand le carousel n'a pas encore
        # eu le temps de décoder tous les services
        startup_time = self.stats.get('start_time')
        if startup_time:
            uptime = (datetime.now() - startup_time).total_seconds()
            if uptime < STARTUP_GRACE_PERIOD:
                return  # trop tôt pour alerter

        with self.services_lock:
            for sid, state in self.services.items():

                # Ignorer les services désactivés
                if not state.enabled:
                    continue

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
                if not svc.enabled:
                    continue  # ne pas comptabiliser les services désactivés
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

            # Flush daily uptime en DB toutes les 5 minutes
            if not hasattr(self, '_last_uptime_flush'):
                self._last_uptime_flush = now
            if now - self._last_uptime_flush >= 300:
                channel = self.ens_config.get('channel', '')
                uptime_copy = dict(self.uptime_stats)
                self._last_uptime_flush = now
                threading.Thread(
                    target=self.db.flush_uptime,
                    args=(uptime_copy, channel),
                    daemon=True
                ).start()

            # Flush blocs 30 min : au changement de slot
            # Slot courant = arrondi aux 30 min inférieures
            slot_dt = datetime.now().replace(
                minute=30 if datetime.now().minute >= 30 else 0,
                second=0, microsecond=0
            )
            slot_str = slot_dt.strftime('%Y-%m-%d %H:%M')
            if not hasattr(self, '_current_slot'):
                self._current_slot = slot_str
                self._slot_stats = {}  # {sid: {checks, present}}

            if slot_str != self._current_slot:
                # Nouveau slot → flush l'ancien en DB
                channel = self.ens_config.get('channel', '')
                slot_to_flush = self._current_slot
                stats_to_flush = dict(self._slot_stats)
                self._current_slot = slot_str
                self._slot_stats = {}

                def _do_flush(slot, stats, ch):
                    for s, v in stats.items():
                        self.db.flush_uptime_block(
                            sid=s, channel=ch, slot=slot,
                            checks=v['checks'], present=v['present']
                        )
                threading.Thread(
                    target=_do_flush,
                    args=(slot_to_flush, stats_to_flush, channel),
                    daemon=True
                ).start()

            # Accumuler les stats du slot courant
            for sid, svc in services_snap.items():
                if sid not in self._slot_stats:
                    self._slot_stats[sid] = {'checks': 0, 'present': 0}
                self._slot_stats[sid]['checks'] += 1
                if svc.present:
                    self._slot_stats[sid]['present'] += 1

    # ═══════════════════════════════════════════════════════════════════════
    # Streaming Icecast
    # ═══════════════════════════════════════════════════════════════════════


    def _kill_all_streaming(self):
        """Tue TOUS les ffmpeg streaming vers Icecast sans exception."""
        import subprocess as sp, time
        sp.run(['pkill', '-9', '-f', 'icecast://'], capture_output=True)
        sp.run(['pkill', '-9', '-f', 'content_type audio/mpeg'], capture_output=True)
        # Attendre qu'Icecast libère le mount
        time.sleep(1.5)
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

            # Demander à welle-cli (welle-monitor fork) de décoder immédiatement
            # ce service sans attendre la rotation du carousel.
            # Réduit le délai de lancement du player de ~10s à ~2s.
            try:
                result = subprocess.run(
                    ['curl', '-s', '-X', 'POST', '--max-time', '2',
                     '-o', '/dev/null', '-w', '%{http_code}',
                     f"{self.welle_url}/carousel/pin/{sid}"],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0 and result.stdout.strip() == '200':
                    logger.debug(f"Carousel pin {sid} : OK")
                else:
                    logger.debug(f"Carousel pin {sid} : non supporté (welle-cli standard)")
            except Exception:
                pass  # welle-cli standard sans le fork — pas grave

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

    def _watchdog_system(self):
        """Vérifie que welle-cli répond et relance si nécessaire.
        Utilise curl via subprocess pour un timeout hard indépendant de requests/urllib3.
        Le verrou _relaunch_lock empêche les relances simultanées.
        """
        # Si une relance est déjà en cours, ne rien faire
        if not self._relaunch_lock.acquire(blocking=False):
            logger.debug("Watchdog : relance déjà en cours, ignoré")
            return

        ok = False
        try:
            # /status ne freeze jamais (n'acquiert pas rx_mut)
            # Compatible welle-monitor fork ; fallback /mux.json pour welle-cli standard
            result = subprocess.run(
                ['curl', '-s', '--max-time', '4',
                 '-o', '/dev/null', '-w', '%{http_code}',
                 f"{self.welle_url}/status"],
                capture_output=True, text=True, timeout=6
            )
            if result.returncode == 0 and result.stdout.strip() == '200':
                ok = True
            else:
                # Fallback /mux.json
                result = subprocess.run(
                    ['curl', '-s', '--max-time', '4',
                     '-o', '/dev/null', '-w', '%{http_code}',
                     f"{self.welle_url}/mux.json"],
                    capture_output=True, text=True, timeout=6
                )
                ok = result.returncode == 0 and result.stdout.strip() == '200'
        except Exception as e:
            ok = False
            logger.debug(f"Watchdog curl exception : {e}")

        if ok:
            # welle-cli répond — libérer le lock immédiatement
            self._relaunch_lock.release()
            return

        # welle-cli KO — tentative de reset doux via POST /reset (welle-monitor fork)
        # avant de recourir au pkill brutal qui coupe le streaming Icecast
        logger.warning("Watchdog : welle-cli KO — tentative reset doux via /reset")
        reset_ok = False
        try:
            result = subprocess.run(
                ['curl', '-s', '-X', 'POST', '--max-time', '3', '--no-keepalive',
                 '-H', 'Connection: close',
                 '-w', '%{http_code}', '-o', '/dev/null',
                 f"{self.welle_url}/reset"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip() == '200':
                logger.info("Watchdog : reset doux envoyé — attente 8s")
                time.sleep(8)
                # Vérifier si le reset a suffi
                check = subprocess.run(
                    ['curl', '-s', '--max-time', '3', '--no-keepalive',
                     '-H', 'Connection: close',
                     '-o', '/dev/null', '-w', '%{http_code}',
                     f"{self.welle_url}/status"],
                    capture_output=True, text=True, timeout=5
                )
                if check.returncode == 0 and check.stdout.strip() == '200':
                    logger.info("Watchdog : reset doux efficace — welle-cli récupéré")
                    reset_ok = True
                    self._relaunch_lock.release()
                    return
                else:
                    logger.warning("Watchdog : reset doux insuffisant — passage au pkill")
        except Exception as e:
            logger.debug(f"Watchdog reset doux exception : {e}")

        if not reset_ok:
            # Relance forcée : pkill + relaunch
            logger.warning("Watchdog : relance forcée (pkill -9 welle-cli)")
            try:
                if self.welle_process:
                    self.welle_process.kill()
            except Exception:
                pass
            os.system("pkill -9 welle-cli 2>/dev/null")
            time.sleep(2)
            self._relaunch_welle_and_wait()

    def _relaunch_welle_and_wait(self):
        """Relance welle-cli et attend qu'il soit prêt. Libère _relaunch_lock à la fin."""
        try:
            t = threading.Thread(target=self._launch_welle_cli, daemon=True)
            t.start()
            if self._wait_for_welle():
                logger.info("Watchdog : welle-cli relancé avec succès")
                # Réinitialiser la grace period pour éviter les fausses alertes
                # après une relance watchdog (carousel pas encore passé partout)
                with self.stats_lock:
                    self.stats['start_time'] = datetime.now()
                logger.info("Grace period réinitialisée après relance watchdog")
            else:
                logger.error("Watchdog : welle-cli ne répond toujours pas après relance")
        finally:
            try:
                self._relaunch_lock.release()
            except RuntimeError:
                pass  # déjà libéré

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
        self.audio_time   = 0      # timestamp Unix du dernier niveau audio (welle-cli)
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


        # Surveillance
        self.present         = True
        self.last_seen       = time.time()
        self.lost_start      = None
        self.lost_alert_sent = False
        self.silence_start   = None
        self.silence_alert_sent = False
        self.enabled         = True   # False → pas d'alertes ni uptime

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
            'audio_time':     self.audio_time,
            'subchannel_id':  self.subchannel_id,
            'prot_info':      self.prot_info,
            'audio_srate':    self.audio_srate,
            'present':        self.present,
            'silence':        self.silence_start is not None,
            'enabled':        self.enabled,
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

        }
