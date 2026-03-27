# DAB+ Monitor

Interface de surveillance DAB+ pour radio associative, basée sur un Raspberry Pi 4 et une clé RTL-SDR.

Développé et maintenu par [Graffiti Radio](https://graffitiradio.fr) — La Roche-sur-Yon, Vendée (France).

![Dashboard DAB+ Monitor](docs/screenshot.png)

## Présentation

DAB+ Monitor permet de surveiller en continu un multiplex DAB+ : niveaux audio, DLS (titre en cours), slideshow, métriques RF (SNR, FIC quality, gain), alertes email en cas de silence ou de perte de service, et écoute du bouquet via un player intégré.

## Fonctionnalités

- **Dashboard en temps réel** : SNR, FIC quality, gain RTL-SDR, correction de fréquence (moyenne glissante 15s)
- **Grille des services** : tous les services du bouquet avec niveaux audio et DLS
- **Panneau de détail** : niveaux L/R, slideshow MOT, compteurs d'erreurs AAC/Frame/RS, infos techniques
- **Player audio intégré** : écoute du service sélectionné via Icecast2
- **Alertes email** : silence audio, perte de service, ensemble dégradé
- **Scanner Band III** : découverte automatique des multiplex disponibles
- **Statistiques uptime** : taux de présence de chaque service sur 24h
- **Historique alertes** : 24h glissantes
- **Export/import de configuration**
- **Authentification** par login/mot de passe

## Architecture

```
RTL-SDR Blog V4
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

- Raspberry Pi 4 (4 Go RAM)
- RTL-SDR Blog V4 (Rafael Micro R828D)
- Canal 9A — 202.928 MHz — LA ROCHE SUR YON

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

### 2. Icecast2

```bash
sudo apt install icecast2
```

Configurer `/etc/icecast2/icecast.xml` avec un mountpoint `/dabmonitor`.

### 3. DAB+ Monitor

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

### 4. Service systemd

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
    "sender_password": "password",
    "recipient_emails": ["admin@example.com"]
  }
}
```

### `carousel_size`

- `0` : mode `-D` (décode tous les services simultanément) — ~300% CPU sur Pi4
- `1` : mode `-C 1` (carousel, un service à la fois) — ~65% CPU sur Pi4 ✅ recommandé

## Dépendance welle-monitor

Ce projet utilise [welle-monitor](https://github.com/LyonelB/welle-monitor), un fork de [welle.io](https://github.com/AlbrechtL/welle.io) avec des améliorations spécifiques au monitoring 24h/24 :

- `std::timed_mutex` sur `rx_mut` : résout le freeze HTTP en signal dégradé
- `GET /status` : endpoint léger pour le watchdog
- `POST /reset` : redémarrage du décodeur sans tuer le processus
- `POST /carousel/pin/<sid>` : force le décodage immédiat d'un service
- `server_time` et `current_carousel_sid` dans `mux.json`

## Licence

GPL-2.0-or-later

## Crédits

- [welle.io](https://github.com/AlbrechtL/welle.io) — Albrecht Lohofener & Matthias P. Braendli
- Développement DAB+ Monitor : Lyonel B. — [Graffiti Radio](https://graffitiradio.fr)
