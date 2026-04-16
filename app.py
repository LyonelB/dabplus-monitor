#!/usr/bin/env python3
"""
Application Flask — DAB+ Monitor
"""

import requests
from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, generate_csrf
import logging
import time
import json
import os
from dotenv import load_dotenv
from dabplus_monitor import DABPlusMonitor
from dabplus_scanner import DABScanner
from auth import Auth

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32).hex()
bcrypt = Bcrypt(app)
csrf = CSRFProtect(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

auth = Auth()
monitor = None
scanner = DABScanner()  # charge scan_results.json si disponible
stats_cache = {'data': None, 'timestamp': 0}

# Token de session unique — "le dernier connecté prend la main"
# Un seul utilisateur actif à la fois. Quand un nouvel utilisateur
# se connecte, l'ancien token est révoqué et sa session devient invalide.
import secrets
_active_session_token = None

# ─────────────────────────────────────────────────────────────────────────────
# SSE
# ─────────────────────────────────────────────────────────────────────────────

def generate_stats_sse():
    while True:
        try:
            if monitor:
                data = monitor.get_stats()
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.2)  # 5 Hz — bon compromis fluidité/charge
        except GeneratorExit:
            break
        except Exception as e:
            logger.error(f"Erreur SSE : {e}")
            time.sleep(1)

# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        if request.is_json:
            data = request.get_json()
            username = data.get('username')
            password = data.get('password')
        else:
            username = request.form.get('username')
            password = request.form.get('password')
        if auth.verify_credentials(username, password):
            global _active_session_token
            # Générer un nouveau token — révoque automatiquement l'ancienne session
            _active_session_token = secrets.token_hex(32)
            session['logged_in'] = True
            session['username'] = username
            session['token'] = _active_session_token
            logger.info(f"Nouvelle session : {username} — ancienne session révoquée")
            if request.is_json:
                return jsonify({'status': 'success', 'redirect': '/'})
            return redirect('/')
        else:
            if request.is_json:
                return jsonify({'status': 'error', 'message': 'Identifiants incorrects'}), 401
            return render_template('login.html', error='Identifiants incorrects')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.before_request
def check_session_token():
    """Vérifie que le token de session est toujours valide.
    Si un autre utilisateur s'est connecté, l'ancien est redirigé vers /login.
    """
    global _active_session_token
    # Exclure les routes publiques
    if request.endpoint in ('login', 'static', 'get_csrf_token', None):
        return None
    if not session.get('logged_in'):
        return None
    # Vérifier que le token de session correspond au token actif
    session_token = session.get('token')
    if _active_session_token and session_token != _active_session_token:
        session.clear()
        logger.info("Session révoquée — nouvel utilisateur connecté")
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'status': 'error', 'message': 'Session expirée', 'redirect': '/login'}), 401
        return redirect(url_for('login') + '?reason=session_expired')

@app.route('/api/csrf-token')
def get_csrf_token():
    return jsonify({'csrf_token': generate_csrf()})

# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
@auth.login_required
def index():
    return render_template('index.html')

@app.route('/config')
@auth.login_required
def config():
    return render_template('config.html')

@app.route('/stats')
@auth.login_required
def stats():
    return render_template('stats.html')

@app.route('/about')
@auth.login_required
def about():
    return render_template('about.html')

# ─────────────────────────────────────────────────────────────────────────────
# API stats
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/stats')
@limiter.exempt
def get_stats():
    now = time.time()
    if stats_cache['data'] and (now - stats_cache['timestamp']) < 0.5:
        return jsonify(stats_cache['data'])
    if monitor:
        data = monitor.get_stats()
        stats_cache['data'] = data
        stats_cache['timestamp'] = now
        return jsonify(data)
    return jsonify({'error': 'Monitor not initialized'}), 503

@app.route('/api/stream/stats')
@limiter.exempt
@csrf.exempt
def stream_stats():
    return Response(
        generate_stats_sse(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/services')
@limiter.exempt
def get_services():
    if monitor:
        return jsonify(monitor.get_services_list())
    return jsonify([])

# ─────────────────────────────────────────────────────────────────────────────
# API contrôle
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/service/switch', methods=['POST'])
@csrf.exempt
@auth.login_required
def switch_service():
    """Change le service streamé vers Icecast."""
    data = request.get_json()
    sid = data.get('sid', '')
    if not sid:
        return jsonify({'status': 'error', 'message': 'SID manquant'}), 400
    if monitor:
        monitor.switch_service(sid)
        return jsonify({'status': 'success', 'sid': sid})
    return jsonify({'status': 'error', 'message': 'Monitor not initialized'}), 503

# ─────────────────────────────────────────────────────────────────────────────
# API scan Band III
# ─────────────────────────────────────────────────────────────────────────────

def generate_scan_sse():
    """SSE temps réel pour la progression du scan."""
    import queue as _queue
    q = _queue.Queue(maxsize=100)
    def cb(status):
        try:
            q.put_nowait(status)
        except Exception:
            pass
    if scanner:
        scanner._progress_cb = cb
    if scanner:
        yield f"data: {json.dumps(scanner.get_status())}\n\n"
    while True:
        try:
            status = q.get(timeout=15)
            yield f"data: {json.dumps(status)}\n\n"
            if not status.get('running') and status.get('current_index', 0) > 0:
                break
        except Exception:
            if scanner:
                yield f"data: {json.dumps(scanner.get_status())}\n\n"
            else:
                break

@app.route('/api/scan/start', methods=['POST'])
@csrf.exempt
@auth.login_required
def scan_start():
    global monitor, scanner
    if scanner and scanner.running:
        return jsonify({'status': 'error', 'message': 'Scan déjà en cours'}), 409
    if monitor and monitor.running:
        monitor.stop()
        time.sleep(2)
    gain = -1
    try:
        with open('config.json') as f:
            cfg = json.load(f)
        gain = int(cfg.get('rtl_sdr', {}).get('gain', -1))
    except Exception:
        pass
    # Effacer le cache des services avant le scan
    # Le scanner repeuplera la DB avec les services découverts
    try:
        if monitor:
            monitor.db.clear_services()
            logger.info("Cache services DB effacé avant scan")
    except Exception as e:
        logger.debug(f"clear_services avant scan : {e}")

    scanner = DABScanner(gain=gain)
    scanner.start_scan()
    return jsonify({'status': 'success', 'message': 'Scan démarré'})

@app.route('/api/scan/stop', methods=['POST'])
@csrf.exempt
@auth.login_required
def scan_stop():
    if scanner:
        scanner.stop_scan()
    return jsonify({'status': 'success'})

@app.route('/api/scan/status')
@limiter.exempt
def scan_status():
    if scanner:
        return jsonify(scanner.get_status())
    return jsonify({'running': False, 'results': [], 'results_count': 0})

@app.route('/api/scan/stream')
@limiter.exempt
@csrf.exempt
def scan_stream():
    return Response(
        generate_scan_sse(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )

@app.route('/api/scan/select', methods=['POST'])
@csrf.exempt
@auth.login_required
def scan_select():
    """Sélectionne un canal après le scan et démarre le monitoring."""
    global monitor
    data = request.get_json()
    channel = data.get('channel', '')
    if not channel:
        return jsonify({'status': 'error', 'message': 'Canal manquant'}), 400
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        result = next(
            (r for r in (scanner.get_results() if scanner else [])
             if r['channel'] == channel),
            None
        )
        cfg['ensemble']['channel'] = channel
        cfg['ensemble']['frequency_mhz'] = result['frequency_mhz'] if result else 0
        with open('config.json', 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    try:
        if monitor:
            monitor.stop()
            time.sleep(2)
        monitor = DABPlusMonitor('config.json')
        monitor.start()
        return jsonify({'status': 'success', 'channel': channel})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/scan/results')
def scan_results():
    if scanner:
        return jsonify(scanner.get_results())
    return jsonify([])

@app.route('/api/restart', methods=['POST'])
@csrf.exempt
@auth.login_required
def restart_monitor():
    global monitor
    try:
        if monitor:
            monitor.stop()
            time.sleep(3)
            monitor.start()
            return jsonify({'status': 'success', 'message': 'Monitoring redémarré'})
        return jsonify({'status': 'error', 'message': 'Monitor not initialized'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/config/full')
def get_config_full():
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        if 'email' in config and 'sender_password' in config['email']:
            config['email']['sender_password'] = '********'
        return jsonify(config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/save', methods=['POST'])
@csrf.exempt
@auth.login_required
def save_config():
    try:
        data = request.get_json()
        with open('config.json', 'r') as f:
            config = json.load(f)
        if 'ensemble' in data:
            config['ensemble'].update(data['ensemble'])
        if 'monitoring' in data:
            config['monitoring'].update(data['monitoring'])
        if 'email' in data:
            for k, v in data['email'].items():
                if k == 'sender_password' and v == '********':
                    continue
                config['email'][k] = v
        with open('config.json', 'w') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# Proxy stream (depuis welle-cli → Icecast → Flask)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/stream.mp3')
@limiter.exempt
def proxy_stream():
    """Redirige vers le flux Icecast (évite de saturer welle-cli)."""
    import re
    icecast_url = 'http://localhost:8000/dabmonitor'
    if monitor:
        cfg_url = monitor.stream_cfg.get('icecast_url', '')
        if cfg_url.startswith('icecast://'):
            m = re.search(r'@([^/]+)(/.+)$', cfg_url)
            if m:
                icecast_url = f"http://{m.group(1)}{m.group(2)}"

    # Vérifier qu'Icecast a un flux actif (GET rapide)
    try:
        test = requests.get(icecast_url, stream=True, timeout=2)
        ct = test.headers.get('content-type', '')
        test.close()
        if 'audio' not in ct:
            return '', 503
    except Exception:
        return '', 503

    def generate():
        try:
            # timeout=(5, None) : 5s connect, lecture illimitée
            # évite la coupure à ~2min causée par timeout=10
            with requests.get(icecast_url, stream=True, timeout=(5, None)) as r:
                if 'audio' not in r.headers.get('content-type', ''):
                    return
                for chunk in r.iter_content(chunk_size=4096):
                    if chunk:
                        yield chunk
        except Exception as e:
            logger.debug(f"Proxy stream Icecast : {e}")

    return app.response_class(
        generate(),
        mimetype='audio/mpeg',
        headers={
            'Cache-Control': 'no-cache, no-store',
            'Access-Control-Allow-Origin': '*',
            'X-Accel-Buffering': 'no',
        }
    )

# ─────────────────────────────────────────────────────────────────────────────
# Démarrage
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/config/export')
@auth.login_required
def export_config():
    """Exporte la config JSON."""
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        if 'email' in cfg and 'sender_password' in cfg['email']:
            cfg['email']['sender_password'] = '********'
        from flask import Response
        return Response(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename=dabplus-monitor-config.json'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/email/test', methods=['POST'])
@csrf.exempt
@auth.login_required
def test_email():
    """Envoie un email de test."""
    try:
        if monitor and hasattr(monitor, 'email_alert'):
            monitor.email_alert.send_alert(
                alert_type="Test email DAB+ Monitor",
                details="Ceci est un email de test envoyé depuis la page de configuration.",
                skip_cooldown=True
            )
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'message': 'Monitor non initialisé'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/welle/carousel/pin/<sid>', methods=['POST'])
@limiter.exempt
@csrf.exempt
def welle_carousel_pin(sid):
    """Proxy vers POST /carousel/pin/<sid> de welle-monitor.
    Force welle-cli à décoder immédiatement le service demandé
    sans attendre la rotation du carousel — réduit le délai de lancement du player.
    Endpoint spécifique au fork welle-monitor ; retourne 503 si non disponible."""
    if not monitor:
        return jsonify({'status': 'error', 'message': 'Monitor non initialisé'}), 503
    try:
        welle_url = monitor.get_welle_url()
        r = requests.post(f"{welle_url}/carousel/pin/{sid}", timeout=2)
        if r.status_code == 200:
            return jsonify({'status': 'pinned', 'sid': sid})
        elif r.status_code == 404:
            return jsonify({'status': 'error', 'message': 'sid_not_found'}), 404
        else:
            return jsonify({'status': 'error', 'message': 'welle-cli error'}), r.status_code
    except Exception as e:
        # welle-cli standard sans le fork — pas grave, le switch fonctionnera quand même
        logger.debug(f"Carousel pin {sid} : {e}")
        return jsonify({'status': 'unavailable'}), 503

@app.route('/slide/<sid>')
@limiter.exempt
@csrf.exempt
def proxy_slide(sid):
    """Proxifie l'image slideshow depuis welle-cli — sans auth."""
    try:
        r = requests.get(f'http://localhost:7979/slide/{sid}', timeout=3)
        if r.status_code == 200:
            return app.response_class(
                r.content,
                mimetype=r.headers.get('Content-Type', 'image/jpeg'),
                headers={'Cache-Control': 'max-age=30'}
            )
    except Exception as e:
        logger.debug(f"Slide {sid} : {e}")
    return '', 404

@app.route('/api/service/uptime/<sid>')
@limiter.exempt
@auth.login_required
def api_service_uptime(sid):
    """Retourne 48 blocs de 30min pour le bar-graphe de présence du service."""
    if not monitor:
        return jsonify([])
    try:
        from datetime import datetime, timedelta
        now = datetime.now()
        channel = monitor.ens_config.get('channel', '')

        # Charger tous les blocs DB pour ce service
        db_blocks = monitor.db.load_uptime_blocks(channel, hours=25)
        svc_blocks = db_blocks.get(sid, {})

        # Slot courant en mémoire
        slot_stats = getattr(monitor, '_slot_stats', {}).get(sid)
        current_slot = getattr(monitor, '_current_slot', None)

        blocks = []
        for i in range(47, -1, -1):
            slot_dt = now - timedelta(minutes=30 * i)
            # Arrondir au slot de 30 min
            slot_dt = slot_dt.replace(
                minute=30 if slot_dt.minute >= 30 else 0,
                second=0, microsecond=0
            )
            slot_str = slot_dt.strftime('%Y-%m-%d %H:%M')
            slot_end = slot_dt + timedelta(minutes=30)

            if slot_str == current_slot and slot_stats:
                # Bloc en cours : données mémoire
                checks  = slot_stats['checks']
                present = slot_stats['present']
                pct = round(present / checks * 100, 1) if checks > 0 else None
                status = 'current'
            elif slot_str in svc_blocks:
                v = svc_blocks[slot_str]
                pct = round(v['present'] / v['checks'] * 100, 1) if v['checks'] > 0 else None
                status = 'done'
            else:
                pct = None
                status = 'no_data'

            blocks.append({
                'slot':   slot_str,
                'end':    slot_end.isoformat(),
                'pct':    pct,
                'status': status,
            })

        return jsonify(blocks)
    except Exception as e:
        logger.error(f"api_service_uptime : {e}")
        return jsonify([])

@app.route('/api/service/<sid>/toggle', methods=['POST'])
@csrf.exempt
@auth.login_required
def api_service_toggle(sid):
    """Active ou désactive la surveillance d'un service."""
    if not monitor:
        return jsonify({'status': 'error'}), 503
    try:
        with monitor.services_lock:
            state = monitor.services.get(sid)
            if not state:
                return jsonify({'status': 'error', 'message': 'Service inconnu'}), 404
            state.enabled = not state.enabled
            enabled = state.enabled
        # Persister en DB
        channel = monitor.ens_config.get('channel', '')
        monitor.db.set_service_enabled(sid, channel, enabled)
        logger.info(f"Service {sid} {'activé' if enabled else 'désactivé'}")
        return jsonify({'status': 'ok', 'sid': sid, 'enabled': enabled})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/services/cached')
@auth.login_required
def api_services_cached():
    """Retourne les services en cache DB pour tous les canaux."""
    if not monitor:
        return jsonify([])
    try:
        # Charger tous les services (pas seulement le canal actif)
        with monitor.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sid, channel, label, bitrate, prot_info,
                       subchannel_id, language, mode, updated_at
                FROM dab_services
                ORDER BY channel, label
            ''')
            return jsonify([dict(row) for row in cursor.fetchall()])
    except Exception as e:
        logger.error(f"Erreur api_services_cached : {e}")
        return jsonify([])

@app.route('/api/logs')
@auth.login_required
def api_logs():
    """Retourne les 100 dernières lignes de logs du service systemd."""
    try:
        import subprocess
        result = subprocess.run(
            ['journalctl', '-u', 'dabplus-monitor', '-n', '100', '--no-pager',
             '--output=short-iso'],
            capture_output=True, text=True, timeout=5
        )
        return jsonify({'logs': result.stdout})
    except Exception as e:
        logger.error(f"Erreur lecture logs : {e}")
        return jsonify({'logs': f'Erreur : {e}'}), 500

@app.route('/api/stats/uptime')
@limiter.exempt
@auth.login_required
def api_uptime():
    if monitor:
        return jsonify(monitor.get_uptime_stats())
    return jsonify([])

@app.route('/api/stats/alerts')
@auth.login_required
def api_alerts():
    """Historique alertes en mémoire (24h glissantes, temps réel)."""
    if monitor:
        return jsonify(monitor.get_alert_history())
    return jsonify([])

@app.route('/api/stats/alerts/db')
@auth.login_required
def api_alerts_db():
    """Historique alertes persisté en base de données (survit aux redémarrages)."""
    if monitor:
        limit = request.args.get('limit', 100, type=int)
        return jsonify(monitor.db.get_alerts_history(limit=limit))
    return jsonify([])

@app.route('/api/stats/alerts/grouped')
@auth.login_required
def api_alerts_grouped():
    """Historique alertes groupées par paires perte/rétablissement."""
    if monitor:
        limit = request.args.get('limit', 50, type=int)
        return jsonify(monitor.db.get_alerts_history_grouped(limit=limit))
    return jsonify([])

try:
    monitor = DABPlusMonitor('config.json')
    monitor.start()
except Exception as e:
    logger.error(f"Erreur démarrage monitor : {e}")

if __name__ == '__main__':
    ssl_context = None
    cert_file, key_file = 'cert.pem', 'key.pem'
    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        logger.info("HTTPS activé")
    try:
        app.run(host='0.0.0.0', port=5000, debug=False,
                threaded=True, ssl_context=ssl_context)
    except KeyboardInterrupt:
        if monitor:
            monitor.stop()
