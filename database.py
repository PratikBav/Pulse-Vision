"""Local storage of heart-rate readings in SQLite.

Only numbers are stored (timestamp, BPM, signal quality, session ID) - never video.
The monitoring log (hardware + camera, once per second) and alerts are stored in
their own tables; the original readings table is unchanged.
"""

import os
import sqlite3

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pulsevision.db")

MONITOR_COLUMNS = [
    "timestamp", "session_id", "hw_status", "hw_port", "hw_simulated", "hw_hr_bpm", "hw_spo2_pct",
    "hw_sensor_ok", "hw_mpu_ok", "hw_accel_g", "hw_motion_g", "hw_gyro_dps",
    "hw_age_s", "sensor2_json",
    "hw_mlx_ok", "hw_ambient_temp_c", "hw_object_temp_c",
    "camera_status", "cam_bpm", "cam_quality",
    "face_detected", "presence", "absent_seconds", "active_alerts",
]
ALERT_COLUMNS = [
    "uid", "session_id", "raised_at", "alert_key", "category", "message", "readings_json",
    "simulated", "delivery_status",
]
NOTIFICATION_COLUMNS = [
    "notification_id", "alert_event_id", "alert_key", "category", "patient_id",
    "parameter", "trigger_reason", "measured_value", "configured_threshold", "event_timestamp",
    "recipient_masked", "channel", "delivery_status", "condition_status", "provider",
    "provider_message_id", "attempt_count", "last_attempt_at", "delivered_at",
    "failure_reason", "simulated", "updated_at",
]


def _ensure_column(conn, table, column, col_type):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def connect():
    """Open the database, creating the folder and the tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readings (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL,
               timestamp  TEXT NOT NULL,
               bpm        REAL NOT NULL,
               quality    TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS monitor_log (
               id             INTEGER PRIMARY KEY AUTOINCREMENT,
               timestamp      TEXT NOT NULL,
               session_id     TEXT,
               hw_status      TEXT,
               hw_port        TEXT,
               hw_simulated   INTEGER,
               hw_hr_bpm      REAL,
               hw_spo2_pct    REAL,
               hw_sensor_ok   INTEGER,
               hw_mpu_ok      INTEGER,
               hw_accel_g     REAL,
               hw_motion_g    REAL,
               hw_gyro_dps    REAL,
               hw_age_s       REAL,
               sensor2_json   TEXT,
               hw_mlx_ok         INTEGER,
               hw_ambient_temp_c REAL,
               hw_object_temp_c  REAL,
               camera_status  TEXT,
               cam_bpm        REAL,
               cam_quality    TEXT,
               face_detected  INTEGER,
               presence       TEXT,
               absent_seconds REAL,
               active_alerts  TEXT
           )"""
    )
    for col, col_type in (("hw_mpu_ok", "INTEGER"), ("hw_accel_g", "REAL"),
                          ("hw_motion_g", "REAL"), ("hw_gyro_dps", "REAL"),
                          ("hw_mlx_ok", "INTEGER"), ("hw_ambient_temp_c", "REAL"),
                          ("hw_object_temp_c", "REAL")):
        _ensure_column(conn, "monitor_log", col, col_type)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               uid             TEXT UNIQUE NOT NULL,
               session_id      TEXT,
               raised_at       TEXT NOT NULL,
               alert_key       TEXT NOT NULL,
               category        TEXT NOT NULL,
               message         TEXT NOT NULL,
               readings_json   TEXT,
               simulated       INTEGER,
               delivery_status TEXT,
               cleared_at      TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
               notification_id TEXT PRIMARY KEY,
               alert_event_id TEXT NOT NULL,
               alert_key TEXT NOT NULL,
               category TEXT NOT NULL,
               patient_id TEXT,
               parameter TEXT,
               trigger_reason TEXT,
               measured_value TEXT,
               configured_threshold TEXT,
               event_timestamp TEXT NOT NULL,
               recipient_masked TEXT,
               channel TEXT,
               delivery_status TEXT NOT NULL,
               condition_status TEXT NOT NULL,
               provider TEXT,
               provider_message_id TEXT,
               attempt_count INTEGER NOT NULL DEFAULT 0,
               last_attempt_at TEXT,
               delivered_at TEXT,
               failure_reason TEXT,
               simulated INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notification_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               notification_id TEXT NOT NULL,
               status TEXT NOT NULL,
               event_timestamp TEXT NOT NULL,
               detail TEXT,
               firebase_event_id TEXT UNIQUE
           )"""
    )
    _ensure_column(conn, "notifications", "trigger_reason", "TEXT")
    _ensure_column(conn, "notification_events", "firebase_event_id", "TEXT")
    return conn


def save_readings(session_id, readings):
    """Save a list of readings, each a dict with 'timestamp', 'bpm' and 'quality'."""
    with connect() as conn:
        conn.executemany(
            "INSERT INTO readings (session_id, timestamp, bpm, quality) VALUES (?, ?, ?, ?)",
            [(session_id, r["timestamp"], r["bpm"], r["quality"]) for r in readings],
        )
    conn.close()


def load_readings(session_id=None):
    """Return saved readings as a DataFrame - for one session, or all sessions if None."""
    query = "SELECT session_id, timestamp, bpm, quality FROM readings"
    params = ()
    if session_id is not None:
        query += " WHERE session_id = ?"
        params = (session_id,)
    with connect() as conn:
        df = pd.read_sql_query(query + " ORDER BY timestamp", conn, params=params)
    conn.close()
    return df


def insert_monitor_rows(rows):
    """Append monitoring rows (dicts with MONITOR_COLUMNS keys; missing values may be None)."""
    placeholders = ", ".join("?" for _ in MONITOR_COLUMNS)
    with connect() as conn:
        conn.executemany(
            f"INSERT INTO monitor_log ({', '.join(MONITOR_COLUMNS)}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in MONITOR_COLUMNS) for r in rows],
        )
    conn.close()


def insert_alert(row):
    """Append one alert (dict with ALERT_COLUMNS keys)."""
    placeholders = ", ".join("?" for _ in ALERT_COLUMNS)
    with connect() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO alerts ({', '.join(ALERT_COLUMNS)}) VALUES ({placeholders})",
            tuple(row.get(c) for c in ALERT_COLUMNS),
        )
    conn.close()


def update_alert(uid, delivery_status=None, cleared_at=None):
    """Record the email delivery result and/or the time an alert cleared."""
    with connect() as conn:
        if delivery_status is not None:
            conn.execute("UPDATE alerts SET delivery_status = ? WHERE uid = ?", (delivery_status, uid))
        if cleared_at is not None:
            conn.execute("UPDATE alerts SET cleared_at = ? WHERE uid = ?", (cleared_at, uid))
    conn.close()


def load_monitor_log(session_id=None, limit=None):
    """Return the monitoring log as a DataFrame (newest last)."""
    query = "SELECT * FROM monitor_log"
    params = []
    if session_id is not None:
        query += " WHERE session_id = ?"
        params.append(session_id)
    query += " ORDER BY id"
    with connect() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df.tail(limit) if limit else df


def load_alerts(limit=20):
    """Return the most recent alerts as a DataFrame (newest first)."""
    with connect() as conn:
        df = pd.read_sql_query(
            "SELECT raised_at, category, message, delivery_status, cleared_at, simulated "
            "FROM alerts ORDER BY id DESC LIMIT ?", conn, params=(limit,))
    conn.close()
    return df


def upsert_notification(row, append_event=True, event_detail=None, event_status=None):
    """Persist current notification state and append a lifecycle audit event."""
    placeholders = ", ".join("?" for _ in NOTIFICATION_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in NOTIFICATION_COLUMNS if c != "notification_id")
    with connect() as conn:
        conn.execute(
            f"INSERT INTO notifications ({', '.join(NOTIFICATION_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(notification_id) DO UPDATE SET {updates}",
            tuple(row.get(c) for c in NOTIFICATION_COLUMNS),
        )
        if append_event:
            conn.execute(
                "INSERT INTO notification_events (notification_id, status, event_timestamp, detail) "
                "VALUES (?, ?, ?, ?)",
                (row["notification_id"], event_status or row["delivery_status"], row["updated_at"], event_detail),
            )


def load_notifications(limit=500):
    """Return recent notification records, newest first."""
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT * FROM notifications ORDER BY event_timestamp DESC, notification_id DESC LIMIT ?",
            conn, params=(int(limit),),
        )


def load_notification_events(notification_id):
    """Return the append-only lifecycle history for one notification."""
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT status, event_timestamp, detail FROM notification_events "
            "WHERE notification_id = ? ORDER BY id",
            conn, params=(notification_id,),
        )


def insert_notification_event(notification_id, status, event_timestamp, detail=None, firebase_event_id=None):
    """Append a lifecycle event once; remote Firebase event IDs are idempotent."""
    with connect() as conn:
        if firebase_event_id:
            conn.execute(
                "INSERT OR IGNORE INTO notification_events "
                "(notification_id, status, event_timestamp, detail, firebase_event_id) VALUES (?, ?, ?, ?, ?)",
                (notification_id, status, event_timestamp, detail, firebase_event_id),
            )
        else:
            conn.execute(
                "INSERT INTO notification_events (notification_id, status, event_timestamp, detail) "
                "VALUES (?, ?, ?, ?)",
                (notification_id, status, event_timestamp, detail),
            )
