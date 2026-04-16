# DAB+ Monitor

Interface de surveillance DAB+ pour radio associative, basée sur un Raspberry Pi 4 et une clé RTL-SDR.

## Présentation

DAB+ Monitor permet de surveiller en continu un multiplex DAB+ : niveaux audio, DLS (titre en cours), slideshow, métriques RF (SNR, FIC quality, gain), alertes email en cas de silence ou de perte de service, et écoute du bouquet via un player intégré.

## Fonctionnalités

- **Dashboard en temps réel** : SNR, FIC quality, gain RTL-SDR, correction de fréquence (moyenne glissante 15s)
- **Grille des services** : tous les services du bouquet avec niveaux audio et DLS
- **Panneau de détail** : niveaux L/R, slideshow MOT, compteurs d'erreurs AAC/Frame/RS, infos techniques
- **Player audio intégré** : écoute du service sélectionné via Icecast2
- **Mode veille automatique** : arrêt du streaming après 15 min, surveillance seule active
- **Alertes email** : silence audio, perte de service, ensemble dégradé (avec rate limiting)
- **Scanner Band III** : découverte automatique des multiplex disponibles
- **Statistiques uptime** : taux de présence de chaque service sur 24h
- **Historique alertes** : 24h glissantes + base de données persistante
- **Export/import de configuration**
- **Authentification** par login/mot de passe (session unique)

## Architecture

```
RTL-SDR (NooElec NESDR SmartEE recommandé)
      ↓
welle-cli (fork welle-monitor)
      ↓ HTTP API /mux.json, /mp3/<SID>
DABPlusMonitor (Python)
      ├── Poll /mux.json toutes les 2s
      ├── Métriques RF + alertes email
      ├── Streaming Icecast2 via ffmpeg
      └── API Flask / SSE → Dashboard
```

## Matériel testé

| Matériel | Statut | Notes |
|----------|--------|-------|
| Raspberry Pi 4 (4 Go RAM) | ✅ Recommandé | Configuration de référence |
| NooElec NESDR SmartEE (R820T) | ✅ Recommandé | SNR élevé, stable 24h/24 |
| RTL-SDR Blog V4 (R828D) | ⚠️ À tester | Instabilité USB observée après plusieurs heures ; peut nécessiter le script de reset USB |
| Hub USB alimenté (Lindy 43228) | ✅ Recommandé | Alimentation séparée pour le dongle RTL-SDR |
| Canal 9A — 202.928 MHz | ✅ Testé | LA ROCHE SUR YON (0xf030) |

> **Note RTL-SDR Blog V4** : des instabilités USB ont été observées après plusieurs heures de fonctionnement continu avec le Blog V4 sur Pi4. Le NooElec NESDR SmartEE (R820T) est plus stable en usage 24h/24. Le Blog V4 peut fonctionner avec le script de reset USB (`/usr/local/bin/rtlsdr-usb-reset.sh`) configuré en sudoers.

## Prérequis logiciels

- Debian 13 (Trixie) / Raspbian
- Python 3.11+
- [welle-monitor](https://github.com/LyonelB/welle-monitor) (fork de welle.io avec améliorations pour le monitoring 24h/24)
- Icecast2
- ffmpeg

## Installation

### 1. welle-monitor (fork welle.io)

```bash
sudo apt install cmake g++ libfaad-dev libfftw3-dev \
    librtlsdr-dev libmp3lame-dev libmpg123-dev libusb-1.0-0-dev xxd

git clone https://github.com/LyonelB/welle-monitor.git
cd welle-monitor/build
cmake .. -DRTLSDR=1 -DBUILD_WELLE_IO=OFF -DBUILD_WELLE_CLI=ON
make -j4
sudo cp welle-cli /usr/bin/welle-cli
```

### 2. Script de reset USB (recommandé)

Nécessaire pour le RTL-SDR Blog V4, optionnel pour les autres dongles :

```bash
sudo tee /usr/local/bin/rtlsdr-usb-reset.sh > /dev/null << 'EOF'
#!/bin/bash
for dev in /sys/bus/usb/devices/*/product; do
  if grep -qi RTL "$dev" 2>/dev/null; then
    devpath=$(dirname "$dev")
    echo 0 > "$devpath/authorized"
    sleep 3
    echo 1 > "$devpath/authorized"
  fi
done
EOF
sudo chmod +x /usr/local/bin/rtlsdr-usb-reset.sh
echo 'graffiti ALL=(ALL) NOPASSWD: /usr/local/bin/rtlsdr-usb-reset.sh' | \
  sudo tee /etc/sudoers.d/rtlsdr-reset
sudo chmod 440 /etc/sudoers.d/rtlsdr-reset
```

### 3. Icecast2

```bash
sudo apt install icecast2
```

Configurer `/etc/icecast2/icecast.xml` avec un mountpoint `/dabmonitor`.

### 4. DAB+ Monitor

```bash
git clone https://github.com/LyonelB/dabplus-monitor.git
cd dabplus-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copier et adapter la configuration :
```bash
cp config.example.json config.json
nano config.json
```

Générer les credentials :
```bash
python3 -c "from auth import Auth; a = Auth(); a.set_password('admin', 'votre_mot_de_passe')"
```

### 5. Service systemd

```bash
sudo cp dabplus-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dabplus-monitor
sudo systemctl start dabplus-monitor
```

## Configuration

Le fichier `config.json` contient :

```json
{
  "ensemble": {
    "channel": "9A",
    "frequency_mhz": 202.928
  },
  "rtl_sdr": {
    "gain": -1,
    "ppm_error": 0
  },
  "welle_cli": {
    "port": 7979,
    "poll_interval": 2,
    "carousel_size": 1
  },
  "monitoring": {
    "snr_threshold": 5.0,
    "fic_quality_threshold": 80,
    "audio_silence_threshold": -60,
    "audio_silence_duration": 30,
    "service_loss_duration": 60
  },
  "streaming": {
    "enabled": true,
    "icecast_url": "icecast://source:password@localhost:8000/dabmonitor",
    "bitrate": "128k",
    "output_rate": "44100"
  },
  "email": {
    "enabled": false,
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "sender_email": "monitor@example.com",
    "sender_password": "app_password_16_chars",
    "recipient_emails": ["admin@example.com"],
    "cooldown_minutes": 30,
    "max_alerts_per_hour": 20
  }
}
```

### `carousel_size`

- `0` : mode `-D` (décode tous les services simultanément) — ~300% CPU sur Pi4
- `1` : mode `-C 1` (carousel, un service à la fois) — ~65% CPU sur Pi4 ✅ recommandé

### Alertes email

Le mot de passe doit être un **mot de passe d'application** (App Password) et non le mot de passe du compte. Pour Gmail : Compte Google → Sécurité → Validation en deux étapes → Mots de passe des applications.

## Dépendance welle-monitor

Ce projet utilise [welle-monitor](https://github.com/LyonelB/welle-monitor), un fork de [welle.io](https://github.com/AlbrechtL/welle.io) avec des améliorations spécifiques au monitoring 24h/24 :

- `std::timed_mutex` sur `rx_mut` : résout le freeze HTTP en signal dégradé
- `GET /status` : endpoint léger pour le watchdog, jamais bloquant
- `POST /reset` : redémarrage du décodeur sans tuer le processus
- `POST /carousel/pin/<sid>` : force le décodage immédiat d'un service
- `server_time` et `current_carousel_sid` dans `mux.json`
- `timed_mutex` dans `handle_phs()` : évite le freeze du thread de gestion des services

> Une issue a été ouverte sur le dépôt upstream : [welle.io #913](https://github.com/AlbrechtL/welle.io/issues/913) — freeze `/mux.json` après ~92 min en mode carousel.

## Logs

| Fichier | Contenu |
|---------|---------|
| `logs/gunicorn-error.log` | Logs applicatifs Python (INFO/WARNING/ERROR) |
| `logs/welle-cli.log` | Stdout/stderr de welle-cli |
| `logs/access.log` | Accès HTTP Gunicorn |

Rotation quotidienne via logrotate (`/etc/logrotate.d/dabplus-monitor`), 7 jours, compression.

## Licence

GPL-2.0-or-later

## Crédits

- [welle.io](https://github.com/AlbrechtL/welle.io) — Albrecht Lohofener & Matthias P. Braendli
- Développement DAB+ Monitor : Lyonel B. — [Graffiti Radio](https://graffitiradio.fr)
