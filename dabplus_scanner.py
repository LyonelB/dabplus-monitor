#!/usr/bin/env python3
"""
Scanner DAB+ Band III — DAB+ Monitor
Itère sur tous les canaux Band III, tente une réception sur chacun
et retourne la liste des ensembles détectés.
"""

import subprocess
import threading
import requests
import logging
import time
import json
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# Import optionnel — le scanner peut tourner sans la DB
try:
    from database import DABDatabase
    _db = DABDatabase('dab_monitor.db')
except Exception:
    _db = None

# ── Canaux Band III complets (France / Europe) ───────────────────────────────
BAND_III_CHANNELS = {
    '5A':  174928, '5B':  176640, '5C':  178352, '5D':  180064,
    '6A':  181936, '6B':  183648, '6C':  185360, '6D':  187072,
    '7A':  188928, '7B':  190640, '7C':  192352, '7D':  194064,
    '8A':  195936, '8B':  197648, '8C':  199360, '8D':  201072,
    '9A':  202928, '9B':  204640, '9C':  206352, '9D':  208064,
    '10A': 209936, '10B': 211648, '10C': 213360, '10D': 215072,
    '11A': 216928, '11B': 218640, '11C': 220352, '11D': 222064,
    '12A': 223936, '12B': 225648, '12C': 227360, '12D': 229072,
    '13A': 230784, '13B': 232496, '13C': 234208, '13D': 235776,
    '13E': 237488, '13F': 239200,
}

SCAN_PORT        = 7980    # port dédié au scan (≠ port monitoring 7979)
SCAN_TIMEOUT     = 8       # secondes d'attente par canal
SCAN_SETTLE      = 3       # secondes avant première interrogation
SCAN_RETRIES     = 3       # tentatives GET /mux.json
SCAN_RESULTS_FILE = 'scan_results.json'


class DABScanner:
    """
    Scan séquentiel de tous les canaux Band III.
    Expose l'état en temps réel via get_status().
    """

    def __init__(self, gain: int = -1):
        self.gain    = gain
        self.running = False
        self.results = []        # canaux détectés
        self.current_channel  = ''
        self.current_index    = 0
        self.total_channels   = len(BAND_III_CHANNELS)
        self.scan_thread      = None
        self._process         = None
        self._lock            = threading.Lock()
        self._progress_cb     = None   # callback(channel, index, total, found)

        # Charger les résultats précédents si disponibles
        self._load_previous_results()

    # ═══════════════════════════════════════════════════════════════════════
    # API publique
    # ═══════════════════════════════════════════════════════════════════════

    def start_scan(self, progress_callback=None):
        """Lance le scan en arrière-plan. progress_callback(status_dict) appelé à chaque canal."""
        if self.running:
            logger.warning("Scan déjà en cours")
            return False

        self._progress_cb = progress_callback
        self.running      = True
        self.results      = []
        self.current_index = 0

        self.scan_thread = threading.Thread(
            target=self._scan_loop,
            daemon=True,
            name='DABScanner'
        )
        self.scan_thread.start()
        logger.info("Scan Band III démarré")
        return True

    def stop_scan(self):
        """Arrête le scan en cours."""
        self.running = False
        self._kill_welle()
        logger.info("Scan interrompu")

    def get_status(self) -> dict:
        """Retourne l'état courant du scan."""
        with self._lock:
            return {
                'running':         self.running,
                'current_channel': self.current_channel,
                'current_index':   self.current_index,
                'total_channels':  self.total_channels,
                'progress_pct':    round(self.current_index / self.total_channels * 100),
                'results':         list(self.results),
                'results_count':   len(self.results),
            }

    def get_results(self) -> list:
        """Retourne la liste des canaux détectés."""
        with self._lock:
            return list(self.results)

    def has_previous_results(self) -> bool:
        return len(self.results) > 0

    # ═══════════════════════════════════════════════════════════════════════
    # Boucle de scan
    # ═══════════════════════════════════════════════════════════════════════

    def _scan_loop(self):
        channels = list(BAND_III_CHANNELS.keys())

        try:
            for i, channel in enumerate(channels):
                if not self.running:
                    break

                with self._lock:
                    self.current_channel = channel
                    self.current_index   = i + 1

                logger.info(f"Scan {channel} ({i+1}/{len(channels)})...")

                result = self._probe_channel(channel)

                if result:
                    with self._lock:
                        self.results.append(result)
                    logger.info(
                        f"✅ {channel} — {result['ensemble_label']} "
                        f"({result['nb_services']} services, SNR {result['snr']} dB)"
                    )
                    # Sauvegarder les services en DB pour le démarrage rapide
                    if _db:
                        try:
                            # Effacer les anciens services de ce canal
                            _db.clear_services(channel)
                            # Sauvegarder les services détaillés
                            for svc in result.get('services_full', []):
                                if svc.get('sid'):
                                    _db.save_service(
                                        sid=svc['sid'],
                                        channel=channel,
                                        label=svc['label'],
                                        bitrate=svc.get('bitrate', 0),
                                        prot_info=svc.get('prot_info', ''),
                                        subchannel_id=svc.get('subchannel_id', 0),
                                        language=svc.get('language', ''),
                                        mode=svc.get('mode', 'DAB+'),
                                        url_mp3=svc.get('url_mp3', f'/mp3/{svc["sid"]}'),
                                    )
                            logger.info(f"DB : {len(result.get('services_full', []))} services sauvegardés pour {channel}")
                        except Exception as e:
                            logger.debug(f"Erreur save services DB : {e}")
                else:
                    logger.debug(f"❌ {channel} — rien")

                # Notifier le callback (SSE)
                if self._progress_cb:
                    try:
                        self._progress_cb(self.get_status())
                    except Exception as e:
                        logger.debug(f"Callback erreur : {e}")

        finally:
            self._kill_welle()
            self.running = False

            with self._lock:
                self.current_channel = ''

            self._save_results()

            logger.info(
                f"Scan terminé — {len(self.results)} ensemble(s) détecté(s)"
            )

            # Notification finale
            if self._progress_cb:
                try:
                    self._progress_cb(self.get_status())
                except Exception:
                    pass

    def _probe_channel(self, channel: str) -> dict | None:
        """
        Tente de recevoir le canal pendant SCAN_TIMEOUT secondes.
        Retourne un dict avec les infos de l'ensemble, ou None.
        """
        self._kill_welle()
        time.sleep(0.5)

        gain_arg = f"-g {self.gain}" if self.gain >= 0 else "-g -1"
        cmd = (
            f"welle-cli -c {channel} {gain_arg} "
            f"-C 1 -w {SCAN_PORT} 2>/dev/null"
        )

        try:
            self._process = subprocess.Popen(
                cmd,
                shell=True,
                executable='/bin/bash',
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error(f"Erreur lancement welle-cli pour {channel} : {e}")
            return None

        # Attendre que welle-cli s'initialise
        time.sleep(SCAN_SETTLE)

        if not self.running:
            self._kill_welle()
            return None

        # Interroger /mux.json
        url = f"http://localhost:{SCAN_PORT}/mux.json"
        for attempt in range(SCAN_RETRIES):
            if not self.running:
                break
            try:
                r = requests.get(url, timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    result = self._extract_ensemble_info(channel, data)
                    if result:
                        self._kill_welle()
                        return result
            except Exception:
                pass
            time.sleep(1)

        self._kill_welle()
        return None

    def _extract_ensemble_info(self, channel: str, data: dict) -> dict | None:
        """Extrait les infos utiles du mux.json."""
        try:
            ensemble = data.get('ensemble', {})
            label    = ensemble.get('label', {}).get('label', '').strip()
            eid      = ensemble.get('id', '')
            demod    = data.get('demodulator', {})
            snr      = float(demod.get('snr', 0))
            services = data.get('services', [])

            # Considérer valide si SNR > 3 dB et ensemble identifié
            if snr < 3.0 or not label:
                return None

            service_list = []
            for s in services:
                svc_label = s.get('label', {}).get('label', '').strip()
                svc_sid   = s.get('sid', '')
                if not svc_label or svc_label in ('..', '.'):
                    continue
                # Extraire détails techniques
                components = s.get('components', [])
                bitrate = 0
                prot_info = ''
                mode = 'DAB+'
                subchannel_id = 0
                for comp in components:
                    sub = comp.get('subchannel', {})
                    if sub.get('bitrate'):
                        bitrate = int(sub.get('bitrate', 0))
                    if sub.get('protection'):
                        prot_info = str(sub.get('protection', ''))
                    if sub.get('subchid') is not None:
                        subchannel_id = int(sub.get('subchid', 0))
                    if comp.get('ascty'):
                        mode = str(comp.get('ascty', 'DAB+'))
                service_list.append({
                    'sid':          svc_sid,
                    'label':        svc_label,
                    'bitrate':      bitrate,
                    'prot_info':    prot_info,
                    'mode':         mode,
                    'subchannel_id': subchannel_id,
                    'language':     s.get('languagestring', ''),
                    'url_mp3':      s.get('url_mp3', f'/mp3/{svc_sid}') or f'/mp3/{svc_sid}',
                })

            freq_khz = BAND_III_CHANNELS.get(channel, 0)

            return {
                'channel':        channel,
                'frequency_mhz':  round(freq_khz / 1000, 3),
                'ensemble_label': label,
                'ensemble_id':    eid,
                'snr':            round(snr, 1),
                'nb_services':    len(service_list),
                'services':       [s['label'] for s in service_list[:10]],
                'services_full':  service_list,  # données complètes pour la DB
                'scanned_at':     datetime.now().strftime('%d/%m/%Y %H:%M'),
            }
        except Exception as e:
            logger.debug(f"Erreur extraction {channel} : {e}")
            return None

    # ═══════════════════════════════════════════════════════════════════════
    # Utilitaires
    # ═══════════════════════════════════════════════════════════════════════

    def _kill_welle(self):
        """Tue le processus welle-cli du scan."""
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None
        # Sécurité : tuer tout welle-cli sur le port scan
        os.system(f"fuser -k {SCAN_PORT}/tcp 2>/dev/null")

    def _save_results(self):
        """Sauvegarde les résultats en JSON."""
        try:
            with open(SCAN_RESULTS_FILE, 'w') as f:
                json.dump({
                    'scanned_at': datetime.now().isoformat(),
                    'results':    self.results,
                }, f, indent=2, ensure_ascii=False)
            logger.info(f"Résultats sauvegardés → {SCAN_RESULTS_FILE}")
        except Exception as e:
            logger.warning(f"Impossible de sauvegarder : {e}")

    def _load_previous_results(self):
        """Charge les résultats du dernier scan si disponibles."""
        try:
            if os.path.exists(SCAN_RESULTS_FILE):
                with open(SCAN_RESULTS_FILE, 'r') as f:
                    data = json.load(f)
                self.results = data.get('results', [])
                if self.results:
                    logger.info(
                        f"Résultats précédents chargés : "
                        f"{len(self.results)} canal(aux) — "
                        f"{data.get('scanned_at', '?')}"
                    )
        except Exception as e:
            logger.debug(f"Pas de résultats précédents : {e}")
