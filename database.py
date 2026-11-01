"""Local storage of heart-rate readings and alerts in SQLite.

Readings store only numbers (timestamp, BPM, signal quality, session ID). Alerts store
what happened, the condition that fired and the readings at that moment - never video.
The alerts table is also how PulseVision passes alerts to the Notification app.
"""

import os
import sqlite3

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pulsevision.db")

# Columns added to the alerts table after its first version (added automatically to old databases)
ALERT_EXTRA_COLUMNS = {
    "condition": "TEXT",      # the rule that fired, with its limit
    "details": "TEXT",        # the readings at that moment
    "bpm": "REAL",            # current (or last known) heart rate
    "quality": "TEXT",        # signal quality of that heart rate
    "notify_status": "TEXT",  # set by the Notification app, e.g. "Shown"
    "notified_at": "TEXT",    # when the Notification app handled it (NULL = not yet)
}


def connect():
    """Open the database, creating the folder and the tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # timeout: two apps use the database, so wait briefly if the other one is writing
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readings (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL,
               timestamp  TEXT NOT NULL,
               bpm        REAL NOT NULL,
               quality    TEXT NOT NULL
           )"""
    )
    extra = ",\n".join(f"{column} {column_type}" for column, column_type in ALERT_EXTRA_COLUMNS.items())
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS alerts (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL,
               timestamp  TEXT NOT NULL,
               kind       TEXT NOT NULL,
               message    TEXT NOT NULL,
               {extra}
           )"""
    )
    # Upgrade an alerts table made by an older version of PulseVision
    existing = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    for column, column_type in ALERT_EXTRA_COLUMNS.items():
        if column not in existing:
            try:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {column} {column_type}")
            except sqlite3.OperationalError as error:
                # The other app added the same column at the same moment - that is fine
                if "duplicate column" not in str(error):
                    raise
    return conn


def save_readings(session_id, readings):
    """Save a list of readings, each a dict with 'timestamp', 'bpm' and 'quality'."""
    with connect() as conn:
        conn.executemany(
            "INSERT INTO readings (session_id, timestamp, bpm, quality) VALUES (?, ?, ?, ?)",
            [(session_id, r["timestamp"], r["bpm"], r["quality"]) for r in readings],
        )
    conn.close()


def save_alert(session_id, timestamp, alert):
    """Log one alert (saved straight away, without the Save session button)."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO alerts (session_id, timestamp, kind, message, condition, details, bpm, quality)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, timestamp, alert.kind, alert.message, alert.condition, alert.details,
             alert.bpm, alert.quality),
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


def load_pending_alerts():
    """Alerts the Notification app has not handled yet, oldest first, as a list of dicts."""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM alerts WHERE notified_at IS NULL ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_alert_notified(alert_id, status, notified_at):
    """Record what the Notification app did with an alert."""
    with connect() as conn:
        conn.execute("UPDATE alerts SET notify_status = ?, notified_at = ? WHERE id = ?",
                     (status, notified_at, alert_id))
    conn.close()


def load_alert_log(limit=500):
    """The most recent alerts with their notification status, newest first."""
    with connect() as conn:
        df = pd.read_sql_query(
            """SELECT id, timestamp, kind, message, condition, details, bpm, quality, session_id,
                      notify_status, notified_at
               FROM alerts ORDER BY id DESC LIMIT ?""",
            conn, params=(limit,))
    conn.close()
    return df
