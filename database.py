"""Local storage of heart-rate readings in SQLite.

Only numbers are stored (timestamp, BPM, signal quality, session ID) - never video.
"""

import os
import sqlite3

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pulsevision.db")


def connect():
    """Open the database, creating the folder and the readings table if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readings (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id TEXT NOT NULL,
               timestamp  TEXT NOT NULL,
               bpm        REAL NOT NULL,
               quality    TEXT NOT NULL
           )"""
    )
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
