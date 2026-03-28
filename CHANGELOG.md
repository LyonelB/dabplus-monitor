## [0.2.2] - 2026-03-28

### Ajouté
- **Cache des services en base de données** : au démarrage, les services sont chargés depuis `dab_monitor.db` sans attendre le décodage FIC — démarrage quasi instantané, plus de "Nouveau service découvert" au boot
- **Le scan peuple la DB** : `dabplus_scanner.py` sauvegarde les services détaillés (SID, label, bitrate, protection, mode) lors du scan — le cache est automatiquement mis à jour
- **Uptime persisté en DB** : les compteurs de présence (checks/present) sont sauvegardés toutes les 5 minutes dans `dab_monitor.db` (table `service_uptime`) — les stats survivent aux redémarrages
- **Page statistiques : services en cache** : nouvelle section affichant les services connus par canal issus du dernier scan
- **Circuit breaker** : après 5 crashs rapides de welle-cli, pause complète de 5 minutes + reset USB du dongle via sysfs — fini la boucle de 30000 crashs par nuit
- **`fuser -k 5000/tcp` dans `ExecStartPre`** : libère proprement le port 5000 au redémarrage du service (exécuté en root via le préfixe `+`)
- **Alertes email de rétablissement audio** : un email `✅ RÉTABLI` est maintenant envoyé quand l'audio revient après un silence ayant déclenché une alerte
- **Alertes persistées en DB** : `_send_alert_tracked` appelle `db.save_alert()` — l'historique survit aux redémarrages
- **Page statistiques : logs système** : affichage des 100 dernières lignes de journalctl avec colorisation ERROR/WARNING/INFO
- **Endpoints `/api/stats/alerts/db`** et **`/api/stats/alerts/grouped`** : accès à l'historique persisté

### Corrigé
- **`_launch_welle_cli` : lock toujours libéré** — `finally` garantit la libération même en cas d'exception
- **`_is_restarting` flag** : `_poll_loop` ne lance plus de watchdog en parallèle si une relance est déjà en cours
- **`fuser -k 5000/tcp`** en root dans `ExecStartPre` — résout le blocage au redémarrage causé par un gunicorn résiduel
- **Soft reset watchdog** : quand `/status` répond OK mais `/mux.json` timeout (rx_mut gelé), `_trigger_soft_reset()` est appelé immédiatement

### Modifié
- **`FMDatabase` → `DABDatabase`**, `fm_monitor.db` → `dab_monitor.db`
- **`get_alerts_history_grouped()`** adapté aux types d'alertes DAB+ (`Silence audio`, `Audio rétabli`, `Service DAB+ absent`, etc.)
- **Scan efface le cache** avant de repeupler la DB — le scan est le seul moyen de mettre à jour les services
