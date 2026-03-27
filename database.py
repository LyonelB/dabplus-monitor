#!/usr/bin/env python3
"""
Module de gestion de la base de données SQLite pour DAB+ Monitor
Enregistre l'historique des alertes et des statistiques de présence
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class DABDatabase:
    def __init__(self, db_path='dab_monitor.db'):
        """Initialise la base de données"""
        self.db_path = db_path
        # Activer WAL mode pour éviter les locks
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.close()
        self.init_database()
    
    @contextmanager
    def get_connection(self):
        """Context manager pour les connexions DB"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Erreur base de données: {e}")
            raise
        finally:
            conn.close()
    
    def init_database(self):
        """Crée les tables si elles n'existent pas"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Table des niveaux audio (enregistrement toutes les 5s)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS audio_levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    level_db REAL NOT NULL,
                    signal_ok BOOLEAN NOT NULL
                )
            ''')
            
            # Index pour les requêtes temporelles
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_audio_timestamp 
                ON audio_levels(timestamp)
            ''')
            
            # Table des alertes
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    alert_type TEXT NOT NULL,
                    level_db REAL,
                    duration_seconds INTEGER,
                    message TEXT,
                    email_sent BOOLEAN DEFAULT 0
                )
            ''')
            
            # Table historique RDS (optionnel, pour analyse)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rds_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ps TEXT,
                    rt TEXT
                )
            ''')
            
            # Table des services DAB+ connus (cache persistant)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dab_services (
                    sid TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    label TEXT,
                    bitrate INTEGER DEFAULT 0,
                    prot_info TEXT,
                    subchannel_id INTEGER DEFAULT 0,
                    language TEXT,
                    mode TEXT DEFAULT "DAB+",
                    url_mp3 TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (sid, channel)
                )
            ''')
            logger.info("Base de données initialisée")
    
    def save_audio_level(self, level_db, signal_ok):
        """Enregistre un niveau audio"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO audio_levels (level_db, signal_ok)
                    VALUES (?, ?)
                ''', (level_db, signal_ok))
        except Exception as e:
            logger.error(f"Erreur sauvegarde niveau: {e}")
    
    def save_alert(self, alert_type, level_db, duration_seconds, message, email_sent=False):
        """Enregistre une alerte"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO alerts (timestamp, alert_type, level_db, duration_seconds, message, email_sent)
                    VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?)
                ''', (alert_type, level_db, duration_seconds, message, email_sent))
                logger.info(f"Alerte enregistrée: {alert_type}")
        except Exception as e:
            logger.error(f"Erreur sauvegarde alerte: {e}")
    
    def save_rds(self, ps, rt):
        """Enregistre les données RDS (optionnel)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO rds_history (ps, rt)
                    VALUES (?, ?)
                ''', (ps, rt))
        except Exception as e:
            logger.error(f"Erreur sauvegarde RDS: {e}")
    
    def get_audio_history(self, hours=24):
        """Récupère l'historique des niveaux audio"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                since = datetime.now() - timedelta(hours=hours)
                
                cursor.execute('''
                    SELECT timestamp, level_db, signal_ok
                    FROM audio_levels
                    WHERE timestamp >= ?
                    ORDER BY timestamp ASC
                ''', (since,))
                
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Erreur récupération historique: {e}")
            return []
    
    def get_alerts_history(self, limit=50):
        """Récupère l'historique des alertes"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT timestamp, alert_type, level_db, duration_seconds, message, email_sent
                    FROM alerts
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))
                
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Erreur récupération alertes: {e}")
            return []
    
    def get_alerts_history_grouped(self, limit=50):
        """Récupère l'historique des alertes DAB+ regroupées par paires (alerte + rétablissement)"""

        # Préfixes des types d'alertes DAB+ et leurs rétablissements correspondants
        # Les types réels sont ex: "Silence audio : GRAFFITI" / "Audio rétabli : GRAFFITI"
        # "Service DAB+ absent : RCF 85" / "Service DAB+ rétabli : RCF 85"
        # "Ensemble DAB+ hors ligne" / "Ensemble DAB+ rétabli"
        ALERT_PREFIXES = [
            ('Silence audio',       'Audio rétabli',          'Silence audio'),
            ('Service DAB+ absent', 'Service DAB+ rétabli',   'Service absent'),
            ('Ensemble DAB+ hors',  'Ensemble DAB+ rétabli',  'Ensemble hors ligne'),
        ]

        def get_category(alert_type):
            """Retourne (catégorie, est_rétablissement, label) pour un type d'alerte."""
            for loss_pfx, restore_pfx, label in ALERT_PREFIXES:
                if alert_type.startswith(restore_pfx):
                    return (label, True)
                if alert_type.startswith(loss_pfx):
                    return (label, False)
            return (alert_type, False)

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT timestamp, alert_type, level_db, duration_seconds, message, email_sent
                    FROM alerts
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit * 3,))

                alerts = [dict(row) for row in cursor.fetchall()]

                grouped = []
                i = 0

                while i < len(alerts):
                    alert = alerts[i]
                    atype = alert['alert_type']
                    label, is_restore = get_category(atype)

                    if is_restore:
                        # Rétablissement : chercher l'alerte de perte correspondante
                        loss_alert = None
                        for j in range(i + 1, min(i + 20, len(alerts))):
                            l2, is_r2 = get_category(alerts[j]['alert_type'])
                            if l2 == label and not is_r2:
                                loss_alert = alerts[j]
                                # Supprimer l'alerte de perte de la liste
                                alerts.pop(j)
                                break

                        if loss_alert:
                            try:
                                dur = int((datetime.fromisoformat(alert['timestamp']) -
                                           datetime.fromisoformat(loss_alert['timestamp'])).total_seconds())
                            except Exception:
                                dur = 0
                            grouped.append({
                                'alert_label': label,
                                'alert_type_loss': loss_alert['alert_type'],
                                'alert_type_restore': atype,
                                'start_time': loss_alert['timestamp'],
                                'end_time': alert['timestamp'],
                                'duration': dur,
                                'message_loss': loss_alert['message'],
                                'message_restore': alert['message'],
                                'emails_sent': (1 if loss_alert['email_sent'] else 0) +
                                               (1 if alert['email_sent'] else 0),
                                'status': 'complete'
                            })
                        else:
                            grouped.append({
                                'alert_label': label,
                                'alert_type_loss': None,
                                'alert_type_restore': atype,
                                'start_time': alert['timestamp'],
                                'end_time': alert['timestamp'],
                                'duration': 0,
                                'message_loss': None,
                                'message_restore': alert['message'],
                                'emails_sent': 1 if alert['email_sent'] else 0,
                                'status': 'restored_only'
                            })
                        i += 1

                    else:
                        # Alerte de perte sans rétablissement encore
                        grouped.append({
                            'alert_label': label,
                            'alert_type_loss': atype,
                            'alert_type_restore': None,
                            'start_time': alert['timestamp'],
                            'end_time': None,
                            'duration': alert['duration_seconds'] or 0,
                            'message_loss': alert['message'],
                            'message_restore': None,
                            'emails_sent': 1 if alert['email_sent'] else 0,
                            'status': 'ongoing'
                        })
                        i += 1

                return grouped[:limit]

        except Exception as e:
            logger.error(f"Erreur récupération alertes groupées: {e}")
            return []
    
    def save_service(self, sid: str, channel: str, label: str, **kwargs):
        """Enregistre ou met à jour un service DAB+ connu."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO dab_services
                        (sid, channel, label, bitrate, prot_info, subchannel_id,
                         language, mode, url_mp3, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    ON CONFLICT(sid, channel) DO UPDATE SET
                        label        = excluded.label,
                        bitrate      = excluded.bitrate,
                        prot_info    = excluded.prot_info,
                        subchannel_id= excluded.subchannel_id,
                        language     = excluded.language,
                        mode         = excluded.mode,
                        url_mp3      = excluded.url_mp3,
                        updated_at   = excluded.updated_at
                ''', (
                    sid, channel, label,
                    kwargs.get('bitrate', 0),
                    kwargs.get('prot_info', ''),
                    kwargs.get('subchannel_id', 0),
                    kwargs.get('language', ''),
                    kwargs.get('mode', 'DAB+'),
                    kwargs.get('url_mp3', f'/mp3/{sid}'),
                ))
        except Exception as e:
            logger.error(f"Erreur save_service : {e}")

    def load_services(self, channel: str) -> list:
        """Charge les services connus pour un canal."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT sid, label, bitrate, prot_info, subchannel_id,
                           language, mode, url_mp3
                    FROM dab_services
                    WHERE channel = ?
                    ORDER BY label
                ''', (channel,))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Erreur load_services : {e}")
            return []

    def clear_services(self, channel: str = None):
        """Supprime le cache des services (un canal ou tous)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if channel:
                    cursor.execute('DELETE FROM dab_services WHERE channel = ?', (channel,))
                else:
                    cursor.execute('DELETE FROM dab_services')
                logger.info(f"Cache services supprimé ({channel or 'tous les canaux'})")
        except Exception as e:
            logger.error(f"Erreur clear_services : {e}")

    def cleanup_old_data(self, days=7):
        """Nettoie les données de plus de X jours"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cutoff = datetime.now() - timedelta(days=days)
                
                cursor.execute('DELETE FROM audio_levels WHERE timestamp < ?', (cutoff,))
                deleted = cursor.rowcount
                
                logger.info(f"Nettoyage: {deleted} enregistrements supprimés")
                return deleted
        except Exception as e:
            logger.error(f"Erreur nettoyage: {e}")
            return 0

