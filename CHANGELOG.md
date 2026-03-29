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
