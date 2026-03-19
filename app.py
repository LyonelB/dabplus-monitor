#!/usr/bin/env python3
"""
Application Flask — DAB+ Monitor
"""
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
            time.sleep(0.5)   # 2 Hz — suffisant pour DAB+
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

@app.route('/api/restart', methods=['POST'])
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
    def generate():
        try:
            with requests.get('http://localhost:8000/dabmonitor', stream=True, timeout=5) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
        except Exception as e:
            logger.error(f"Erreur proxy stream : {e}")

    import requests as req
    return app.response_class(
        generate(),
        mimetype='audio/mpeg',
        headers={
            'Cache-Control': 'no-cache, no-store',
            'Access-Control-Allow-Origin': '*'
        }
    )

# ─────────────────────────────────────────────────────────────────────────────
# Démarrage
# ─────────────────────────────────────────────────────────────────────────────

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
