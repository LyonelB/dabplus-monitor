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

bcrypt  = Bcrypt(app)
csrf    = CSRFProtect(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)
auth = Auth()

monitor    = None
scanner    = DABScanner()  # charge scan_results.json si disponible
stats_cache = {'data': None, 'timestamp': 0}

# ─────────────────────────────────────────────────────────────────────────────
# SSE
# ─────────────────────────────────────────────────────────────────────────────

def generate_stats_sse():
    while True:
        try:
            if monitor:
                data = monitor.get_stats()
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.1)   # 10 Hz — pour VU-mètre fluide
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
            data     = request.get_json()
            username = data.get('username')
            password = data.get('password')
        else:
            username = request.form.get('username')
            password = request.form.get('password')

        if auth.verify_credentials(username, password):
            session['logged_in'] = True
            session['username']  = username
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
        stats_cache['data']      = data
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
    sid  = data.get('sid', '')
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

    # Envoyer l'état initial
    if scanner:
        yield f"data: {json.dumps(scanner.get_status())}\n\n"

    while True:
        try:
            status = q.get(timeout=15)
            yield f"data: {json.dumps(status)}\n\n"
            if not status.get('running') and status.get('current_index', 0) > 0:
                break
        except Exception:
            # Heartbeat
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
    # Arrêter le monitoring pendant le scan
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
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':       'keep-alive',
        }
    )

@app.route('/api/scan/select', methods=['POST'])
@csrf.exempt
@auth.login_required
def scan_select():
    """Sélectionne un canal après le scan et démarre le monitoring."""
    global monitor
    data    = request.get_json()
    channel = data.get('channel', '')
    if not channel:
        return jsonify({'status': 'error', 'message': 'Canal manquant'}), 400

    # Mettre à jour la config avec le canal sélectionné
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)

        result = next(
            (r for r in (scanner.get_results() if scanner else [])
             if r['channel'] == channel),
            None
        )
        cfg['ensemble']['channel']       = channel
        cfg['ensemble']['frequency_mhz'] = result['frequency_mhz'] if result else 0

        with open('config.json', 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    # (Re)démarrer le monitoring sur ce canal
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

    def generate():
        try:
            with requests.get(icecast_url, stream=True, timeout=10) as r:
                if r.status_code != 200:
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


@app.route('/stats')
@auth.login_required
def stats_page():
    return render_template('stats.html')

@app.route('/api/stats/uptime')
@auth.login_required
def api_uptime():
    if monitor:
        return jsonify(monitor.get_uptime_stats())
    return jsonify([])

@app.route('/api/stats/alerts')
@auth.login_required
def api_alerts():
    if monitor:
        return jsonify(monitor.get_alert_history())
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
