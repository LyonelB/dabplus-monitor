# Changelog — DAB+ Monitor

## [0.2.1] - 2026-03-27

### Corrigé
- **`_launch_welle_cli` : lock toujours libéré** — `lock_released` booléen + `finally` garantit que `_launch_lock` est libéré même en cas d'exception entre `Popen` et `release()`, évitant le blocage permanent en boucle de relance
- **Compteur de crashs rapides** — si welle-cli crashe en moins de 3s, un compteur s'incrémente ; au 3ème crash rapide consécutif : reset USB du dongle + attente 20s (évite la saturation du dongle après une coupure de multiplex)
- **Watchdog : détection `rx_mut` gelé** — quand `/status` répond OK mais `/mux.json` timeout, le watchdog déclenche maintenant un reset doux immédiat via `_trigger_soft_reset()` au lieu de conclure faussement que tout va bien
- **`systemd ExecStartPre`** : suppression du `pkill -9 gunicorn` qui tuait le processus systemd lui-même lors du restart

### Ajouté
- **`_trigger_soft_reset()`** — nouvelle méthode qui envoie `POST /reset` à welle-monitor sans tuer le processus ; utilisée quand `rx_mut` est gelé (signal dégradé) et par le watchdog
- **Affichage `current_carousel_sid` dans la sidebar** — ligne "En décodage" en violet, visible uniquement en mode carousel (`-C N`), affiche le nom du service en cours de décodage
- **Favicon 📻** — supprime le spinner navigateur causé par les connexions SSE persistantes

---

## [0.2.0] - 2026-03-26

### Ajouté
- **Période de grâce au démarrage** (180s) : supprime les fausses alertes de silence au lancement, le temps que le carousel passe sur tous les services
- **Endpoint `/api/welle/carousel/pin/<sid>`** : proxy vers welle-monitor fork pour forcer le décodage immédiat du service sélectionné, réduit le délai d'affichage du player
- **Moyenne glissante SNR/FIC/Gain** sur 15 secondes pour un affichage plus stable des métriques RF
- **`current_carousel_sid`** dans les métriques RF : indique le service en cours de décodage en mode carousel
- **`server_time`** dans les données welle-cli : permet de détecter la fraîcheur des données

### Corrigé
- **Freeze VU-mètres et player** : résolu via fork welle-monitor (`std::timed_mutex`)
- **Watchdog robuste** : reset doux via `POST /reset` avant le `pkill -9`, verrou `_relaunch_lock` pour éviter les relances simultanées
- **Watchdog fiable** : utilise `/status` (jamais bloquant) au lieu de `/mux.json`
- **Démarrage non bloquant** : le monitor démarre même si welle-cli ne répond pas encore
- **Timeout stream** : `timeout=(5, None)` sur le proxy Icecast, supprime la coupure à 2 minutes
- **CPU welle-cli** : mode carousel `-C 1` (64% CPU au lieu de 295%)
- **Commande welle-cli** : correction du bug `-C 1w` → `-C 1 -w`
- **Boucle infinie de relances** : `_relaunch_lock` dans `_watchdog_system`
- **Lecture audio** : continue lors du changement d'onglet navigateur
- **Poll timeout** : `timeout=(3, 3)` sur les requêtes `/mux.json`

### Modifié
- **VU-mètre ffmpeg supprimé** : remplacé par les niveaux `audio_l`/`audio_r` de welle-cli
- **Affichage du player** : détection via SSE `active_sid` — affichage quasi instantané
- **Intervalle watchdog** : 30s → 10s

### Fork welle-monitor
Création du fork [LyonelB/welle-monitor](https://github.com/LyonelB/welle-monitor) avec :
- `std::timed_mutex rx_mut` : résout le freeze HTTP en signal dégradé
- `GET /status` : endpoint léger sans `rx_mut`
- `POST /reset` : redémarrage du décodeur sans tuer le processus
- `POST /carousel/pin/<sid>` : force le décodage immédiat d'un service
- `server_time` et `current_carousel_sid` dans `mux.json`

---

## [0.1.0] - 2026-03-20

- Version initiale
- Dashboard temps réel (SNR, FIC quality, gain, services, DLS, slideshow)
- Alertes email (silence audio, perte de service, ensemble dégradé)
- Player audio intégré via Icecast2
- Scanner Band III
- Statistiques uptime 24h
- Authentification login/mot de passe
