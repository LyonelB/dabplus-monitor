## [0.6.0] - 2026-04-16

### Corrigé
- **Crash welle-cli toutes les ~92 min** : stderr/stdout redirigés vers fichier log au lieu de `subprocess.PIPE` — le buffer de 64KB se remplissait et bloquait welle-cli définitivement
- **Freeze /mux.json** : `_poll_loop` ne déclenche plus de relance si `/status` répond `alive:true` — le mutex `rx_mut` occupé n'est plus confondu avec un crash
- **Reset USB inefficace** : le reset sysfs (`echo 0/1 > authorized`) nécessite root ; délégué à `/usr/local/bin/rtlsdr-usb-reset.sh` via sudoers
- **Alertes email envoyées malgré `enabled=False`** : `send_alert()` contournait le check `enabled` sur les rétablissements et `skip_cooldown=True`
- **Compteur crashs rapides non protégé** : `_fast_crash_count` incrémenté avec un lock anonyme sans effet ; remplacé par `_fast_crash_lock` dédié
- **Auto-sélection de service en mode veille** : `_parse_mux_json` ne sélectionne plus de service par défaut quand `_veille_mode=True`

### Ajouté
- **Mode veille automatique** : après 15 min de streaming, le système passe en mode veille (aucun service sélectionné, streaming arrêté, surveillance seule active). Sortie du mode veille par sélection explicite d'un service dans l'interface
- **Log welle-cli dédié** : `logs/welle-cli.log` capture stdout/stderr de welle-cli pour le diagnostic
- **Session unique** : token de session révoqué automatiquement à chaque nouvelle connexion ("le dernier connecté prend la main")
- **Rate limit email** : max N emails/heure toutes alertes confondues (configurable via `max_alerts_per_hour`)
- **`veille_mode`** exposé dans l'API `/api/stats` pour le frontend

### Modifié
- **Seuil circuit breaker** : 5 crashs rapides → 3 (délai entre crashs : 15s → 20s)
- **Délai reset USB** : 6s → 12s pour laisser le dongle se réinitialiser complètement
- **Timer streaming** : vérification toutes les secondes (polling) au lieu d'un check en début de boucle seulement — garantit l'arrêt à exactement 15 min
- **Journal système** : `systemd-journald` configuré en mode persistant (`/var/log/journal`)
- **Logging Gunicorn** : `--capture-output --error-logfile logs/gunicorn-error.log --log-level info`

### Infrastructure
- Passage du dongle **RTL-SDR Blog V4** au **NooElec NESDR SmartEE** (R820T) : SNR +4dB, correction fréquence 10× plus stable, pas d'instabilité USB après plusieurs heures
- Fork **welle-monitor** recompilé avec patch `handle_phs` : `timed_mutex` 5s sur `rx_mut` dans `handle_phs()` pour éviter le freeze du thread de gestion des services
- Hub **Lindy 43228** USB 3.0 installé (alimentation indépendante du Pi)
- Script `/usr/local/bin/rtlsdr-usb-reset.sh` + entrée sudoers `graffiti`
- Logrotate configuré : rotation quotidienne, 7 jours, compression

### Issue GitHub ouverte
- [welle.io #913](https://github.com/AlbrechtL/welle.io/issues/913) : freeze `/mux.json` après ~92 min en mode carousel, crash régulier toutes les 92 min
## [0.2.3] - 2026-03-29

### Ajouté
- **Toggle surveillance par service** : switch on/off dans chaque carte de service — quand désactivé, pas d'alertes silence/absence et pas de comptabilisation uptime. État persisté en DB (`dab_services.enabled`).
- **Bar-graphe présence 24h** : remplace le VU-mètre dans le panneau de détail — 48 blocs de 30 minutes, gris si pas de données, vert/orange/rouge selon le taux de présence. Données stockées en table `service_uptime_blocks`.
- **Services en cache DB fusionnés avec uptime** : la section "Services en cache DB" de la page statistiques affiche maintenant le % de présence directement dans chaque carte de service.
- **Issue upstream welle.io** : signalement du freeze `rx_mut` et des améliorations aux mainteneurs — https://github.com/AlbrechtL/welle.io/issues/911

### Corrigé
- **Circuit breaker : lock tenu pendant la pause** — pendant la pause de 300s, `_launch_lock` est réacquis pour bloquer tout autre thread. Fini les 30000 crashs par nuit.
- **Références VU-mètre supprimées** dans `updateServiceDetail` — causaient une erreur JS silencieuse qui bloquait le chargement du player.
- **`fuser -k 5000/tcp` en root** dans `ExecStartPre` (préfixe `+`) — résout le blocage au redémarrage.

### Modifié
- **Section "Durée de présence"** supprimée de la page statistiques — fusionnée dans "Services en cache DB"
