#!/bin/bash
# ============================================================
# DAB+ Monitor — Script d'installation automatique
# https://github.com/LyonelB/dabplus-monitor
#
# Usage :
#   curl -fsSL https://raw.githubusercontent.com/LyonelB/dabplus-monitor/main/install.sh | bash
#   ou : bash install.sh
#
# Testé sur : Raspberry Pi OS Bookworm / Debian Trixie (aarch64)
# ============================================================

set -e

# ── Couleurs ────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
step()    { echo -e "\n${BOLD}==> $*${NC}"; }

# ── Configuration ───────────────────────────────────────────
INSTALL_DIR="$HOME/dabplus-monitor"
WELLE_DIR="$HOME/welle-monitor"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="dabplus-monitor"
ICECAST_MOUNT="dabmonitor"
ICECAST_SOURCE_PWD="dabsource"
ICECAST_ADMIN_PWD="dabadmin"
ICECAST_RELAY_PWD="dabrelay"
PPM_MEASURE_SECONDS=120

# ── Vérifications préalables ────────────────────────────────
step "Vérifications préalables"

if [ "$EUID" -eq 0 ]; then
    error "Ne pas lancer ce script en root. Utilisez un utilisateur normal avec sudo."
fi

if ! command -v sudo &>/dev/null; then
    error "sudo est requis."
fi

# Vérifier que le dongle RTL-SDR est branché
if ! lsusb | grep -qi "RTL\|0bda:2838\|0bda:2832"; then
    warn "Dongle RTL-SDR non détecté. Branchez-le avant de continuer."
    read -rp "Continuer quand même ? [o/N] " ans
    [[ "$ans" =~ ^[oO]$ ]] || exit 1
fi

success "Prérequis OK"

# ── Mot de passe admin ──────────────────────────────────────
step "Création du compte administrateur"
echo ""
echo "Choisissez un mot de passe pour l'interface web DAB+ Monitor."
while true; do
    read -rsp "  Mot de passe admin : " ADMIN_PASSWORD
    echo ""
    read -rsp "  Confirmer : " ADMIN_PASSWORD2
    echo ""
    if [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD2" ] && [ -n "$ADMIN_PASSWORD" ]; then
        break
    fi
    warn "Les mots de passe ne correspondent pas ou sont vides. Réessayez."
done
success "Mot de passe enregistré"

# ── Dépendances système ──────────────────────────────────────
step "Installation des dépendances système"

sudo apt-get update -qq

sudo apt-get install -y \
    git python3 python3-pip python3-venv \
    cmake g++ \
    libfaad-dev libfftw3-dev librtlsdr-dev \
    libmp3lame-dev libmpg123-dev libusb-1.0-0-dev xxd \
    ffmpeg curl rtl-sdr \
    2>/dev/null

success "Dépendances installées"

# ── Mesure PPM ───────────────────────────────────────────────
step "Mesure de la correction PPM du dongle RTL-SDR (~${PPM_MEASURE_SECONDS}s)"
info "Mesure en cours... (${PPM_MEASURE_SECONDS} secondes)"

PPM_VALUE=0
PPM_LOG=$(mktemp)

timeout $PPM_MEASURE_SECONDS rtl_test -p > "$PPM_LOG" 2>&1 || true

# Extraire la dernière valeur cumulative PPM
PPM_RAW=$(grep "cumulative PPM" "$PPM_LOG" | tail -1 | grep -oE '[-0-9]+$' || echo "0")
PPM_VALUE=${PPM_RAW:-0}
rm -f "$PPM_LOG"

success "PPM mesuré : ${PPM_VALUE} ppm"

# ── Icecast2 ─────────────────────────────────────────────────
step "Installation et configuration d'Icecast2"

# Pré-remplir les réponses debconf pour éviter l'interface interactive
sudo debconf-set-selections << EOF
icecast2 icecast2/icecast-setup boolean true
icecast2 icecast2/hostname string localhost
icecast2 icecast2/sourcepassword string ${ICECAST_SOURCE_PWD}
icecast2 icecast2/relaypassword string ${ICECAST_RELAY_PWD}
icecast2 icecast2/adminpassword string ${ICECAST_ADMIN_PWD}
EOF

DEBIAN_FRONTEND=noninteractive sudo apt-get install -y icecast2 2>/dev/null

# Configurer icecast.xml
sudo tee /etc/icecast2/icecast.xml > /dev/null << EOF
<icecast>
  <location>Earth</location>
  <admin>icemaster@localhost</admin>
  <limits>
    <clients>10</clients>
    <sources>2</sources>
    <threadpool>5</threadpool>
    <queue-size>524288</queue-size>
    <client-timeout>30</client-timeout>
    <header-timeout>15</header-timeout>
    <source-timeout>10</source-timeout>
    <burst-on-connect>1</burst-on-connect>
    <burst-size>65535</burst-size>
  </limits>
  <authentication>
    <source-password>${ICECAST_SOURCE_PWD}</source-password>
    <relay-password>${ICECAST_RELAY_PWD}</relay-password>
    <admin-user>admin</admin-user>
    <admin-password>${ICECAST_ADMIN_PWD}</admin-password>
  </authentication>
  <hostname>localhost</hostname>
  <listen-socket>
    <port>8000</port>
  </listen-socket>
  <mount type="normal">
    <mount-name>/${ICECAST_MOUNT}</mount-name>
    <max-listeners>10</max-listeners>
  </mount>
  <fileserve>1</fileserve>
  <paths>
    <basedir>/usr/share/icecast2</basedir>
    <logdir>/var/log/icecast2</logdir>
    <webroot>/usr/share/icecast2/web</webroot>
    <adminroot>/usr/share/icecast2/admin</adminroot>
    <pidfile>/run/icecast2/icecast2.pid</pidfile>
  </paths>
  <logging>
    <accesslog>access.log</accesslog>
    <errorlog>error.log</errorlog>
    <loglevel>3</loglevel>
    <logsize>10000</logsize>
  </logging>
  <security>
    <chroot>0</chroot>
  </security>
</icecast>
EOF

sudo systemctl enable icecast2
sudo systemctl restart icecast2
sleep 2

if systemctl is-active --quiet icecast2; then
    success "Icecast2 démarré sur le port 8000 (mount: /${ICECAST_MOUNT})"
else
    error "Icecast2 n'a pas démarré. Vérifiez : sudo journalctl -u icecast2"
fi

# ── Script USB reset ─────────────────────────────────────────
step "Configuration du script de reset USB RTL-SDR"

sudo tee /usr/local/bin/rtlsdr-usb-reset.sh > /dev/null << 'USBEOF'
#!/bin/bash
for dev in /sys/bus/usb/devices/*/product; do
  if grep -qi RTL "$dev" 2>/dev/null; then
    devpath=$(dirname "$dev")
    echo 0 > "$devpath/authorized"
    sleep 3
    echo 1 > "$devpath/authorized"
    echo "USB reset: $devpath" >&2
  fi
done
USBEOF

sudo chmod +x /usr/local/bin/rtlsdr-usb-reset.sh

SUDOERS_LINE="${USER} ALL=(ALL) NOPASSWD: /usr/local/bin/rtlsdr-usb-reset.sh"
if ! sudo grep -q "rtlsdr-usb-reset" /etc/sudoers.d/rtlsdr-reset 2>/dev/null; then
    echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/rtlsdr-reset > /dev/null
    sudo chmod 440 /etc/sudoers.d/rtlsdr-reset
fi

success "Script USB reset configuré"

# ── welle-monitor ────────────────────────────────────────────
step "Compilation de welle-monitor (fork welle.io)"

if [ -d "$WELLE_DIR" ]; then
    info "welle-monitor déjà cloné — mise à jour"
    git -C "$WELLE_DIR" pull --ff-only 2>/dev/null || true
else
    git clone https://github.com/LyonelB/welle-monitor.git "$WELLE_DIR"
fi

mkdir -p "$WELLE_DIR/build"
cd "$WELLE_DIR/build"

cmake .. \
    -DRTLSDR=1 \
    -DBUILD_WELLE_IO=OFF \
    -DBUILD_WELLE_CLI=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr/local \
    2>/dev/null

make -j"$(nproc)"
sudo cp welle-cli /usr/bin/welle-cli
cd "$HOME"

success "welle-cli installé : $(welle-cli -h 2>&1 | head -1)"

# ── DAB+ Monitor ─────────────────────────────────────────────
step "Installation de DAB+ Monitor"

if [ -d "$INSTALL_DIR" ]; then
    info "dabplus-monitor déjà présent — mise à jour"
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || true
else
    git clone https://github.com/LyonelB/dabplus-monitor.git "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# Environnement Python
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r requirements.txt -q

# Dossier logs
mkdir -p logs

# Configuration
if [ ! -f config.json ]; then
    cp config.example.json config.json
    # Injecter l'URL Icecast
    python3 - << PYEOF
import json
with open('config.json') as f: c = json.load(f)
c['streaming']['icecast_url'] = 'icecast://source:${ICECAST_SOURCE_PWD}@localhost:8000/${ICECAST_MOUNT}'
with open('config.json', 'w') as f: json.dump(c, f, indent=2, ensure_ascii=False)
PYEOF
    info "config.json créé depuis config.example.json"
else
    info "config.json existant conservé"
fi

# Credentials admin
"$VENV_DIR/bin/python3" - << PYEOF
import sys, json
sys.path.insert(0, '.')
from flask_bcrypt import Bcrypt
_bcrypt = Bcrypt()
password_hash = _bcrypt.generate_password_hash('${ADMIN_PASSWORD}').decode('utf-8')
with open('config.json') as f:
    c = json.load(f)
if 'auth' not in c:
    c['auth'] = {}
c['auth']['username'] = 'admin'
c['auth']['password_hash'] = password_hash
with open('config.json', 'w') as f:
    json.dump(c, f, indent=2, ensure_ascii=False)
print("Compte admin créé")
PYEOF

success "DAB+ Monitor configuré"

# ── Service systemd ──────────────────────────────────────────
step "Configuration du service systemd"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=DAB+ Monitor
After=network.target icecast2.service

[Service]
Environment=RTLSDR_PPM=${PPM_VALUE}
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStartPre=+/bin/bash -c 'fuser -k 5000/tcp 2>/dev/null || true'
ExecStartPre=/bin/bash -c 'pkill -9 welle-cli || true'
ExecStartPre=/bin/sleep 2
ExecStart=${VENV_DIR}/bin/gunicorn \\
  --workers 1 --threads 16 --worker-class gthread \\
  --bind 0.0.0.0:5000 --timeout 300 \\
  --error-logfile ${INSTALL_DIR}/logs/gunicorn-error.log \\
  --capture-output \\
  --log-level info \\
  app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}
sleep 5

if systemctl is-active --quiet ${SERVICE_NAME}; then
    success "Service ${SERVICE_NAME} démarré"
else
    error "Le service n'a pas démarré. Vérifiez : sudo journalctl -u ${SERVICE_NAME}"
fi

# ── Journal persistant ───────────────────────────────────────
step "Activation du journal persistant"
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
sudo systemctl restart systemd-journald

# ── Logrotate ────────────────────────────────────────────────
sudo tee /etc/logrotate.d/${SERVICE_NAME} > /dev/null << EOF
${INSTALL_DIR}/logs/*.log {
    su ${USER} ${USER}
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ── Résumé ───────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║        DAB+ Monitor — Installation terminée      ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Interface web  : ${BOLD}http://${IP}:5000${NC}"
echo -e "  Login          : ${BOLD}admin${NC}"
echo -e "  PPM mesuré     : ${BOLD}${PPM_VALUE} ppm${NC}"
echo -e "  Icecast        : http://${IP}:8000 (mount: /${ICECAST_MOUNT})"
echo ""
echo -e "  Logs           : ${INSTALL_DIR}/logs/gunicorn-error.log"
echo -e "  Service        : sudo systemctl status ${SERVICE_NAME}"
echo ""
echo -e "${YELLOW}À faire après l'installation :${NC}"
echo -e "  1. Ouvrir http://${IP}:5000 et se connecter"
echo -e "  2. Aller dans Configuration → Scanner pour découvrir les canaux DAB+"
echo -e "  3. Sélectionner votre multiplex"
echo ""
