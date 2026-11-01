"""PulseVision Notifications - shows PulseVision alerts as system notifications on this device.

PulseVision saves every alert to the local SQLite database. This app watches the
database and, for each new alert, shows a notification with the condition that fired
and the readings at that moment. It also keeps a log of every alert.

Run it next to PulseVision with:
    streamlit run notification_app.py --server.port 8502
"""

import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from alerts import ABSENT, FLUCTUATION, HIGH_HEART_RATE
from database import DB_PATH, load_alert_log, load_pending_alerts, mark_alert_notified
from notifier import send_system_notification

CHECK_INTERVAL_SECONDS = 1.0
MISSED_AFTER_SECONDS = 60  # alerts older than this when the app starts are logged, not notified
ALERT_KINDS = [ABSENT, HIGH_HEART_RATE, FLUCTUATION]


class NotificationWatcher:
    """Background thread: checks the database every second and notifies new alerts."""

    def __init__(self):
        self.enabled = {ABSENT: True, HIGH_HEART_RATE: True, FLUCTUATION: False}
        self.started = datetime.now()
        self.last_check = None
        self.shown = 0
        self.last_error = None
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.check()
                self.last_error = None
            except Exception as error:  # keep watching even if one check fails
                self.last_error = str(error)
            self.last_check = datetime.now()
            time.sleep(CHECK_INTERVAL_SECONDS)

    def check(self):
        for alert in load_pending_alerts():
            created = datetime.fromisoformat(alert["timestamp"])
            if created < self.started - timedelta(seconds=MISSED_AFTER_SECONDS):
                status = "Logged only - happened while this app was not running"
            elif not self.enabled.get(alert["kind"], False):
                status = "Logged only - notifications off for this alert"
            else:
                error = send_system_notification(*notification_text(alert))
                status = "Notification shown" if error is None else f"Notification failed: {error}"
                if error is None:
                    self.shown += 1
            mark_alert_notified(alert["id"], status, datetime.now().isoformat(timespec="seconds"))


def notification_text(alert):
    """(title, line 1, line 2) of the notification: what happened, why, and the readings."""
    title = f"PulseVision: {alert['kind']}"
    line1 = f"Condition: {alert['condition'] or alert['message']}"
    line2 = f"{alert['details'] or ''} (at {alert['timestamp'][11:]})"
    return title, line1, line2


@st.cache_resource
def get_watcher():
    """Start the watcher once per app process (not once per browser tab)."""
    return NotificationWatcher()


# ---------------------------------------------------------------- Page
st.set_page_config(page_title="PulseVision Notifications", page_icon=":material/notifications_active:",
                   layout="wide")
st.title(":material/notifications_active: Notifications")
st.caption("Shows PulseVision alerts as notifications on this device and keeps a log of them. "
           "Keep this app running next to PulseVision.")

watcher = get_watcher()

with st.sidebar:
    st.header("Notify me about")
    for kind in ALERT_KINDS:
        watcher.enabled[kind] = st.toggle(kind, value=watcher.enabled[kind], key=f"notify_{kind}")
    st.caption("Alerts that are switched off are still logged.")
    st.divider()
    if st.button("Send test notification", icon=":material/send:", width="stretch"):
        error = send_system_notification("PulseVision: test notification",
                                         "Condition: test from the Notification app",
                                         f"Readings: none (sent at {datetime.now():%H:%M:%S})")
        if error is None:
            st.success("Test notification sent.")
        else:
            st.error(f"Could not show the notification: {error}")
    st.divider()
    st.caption(f"Database: `{DB_PATH}`")


@st.fragment(run_every=2)
def live_view():
    """Refreshes every 2 seconds without reloading the whole page."""
    log = load_alert_log()
    today = log[log["timestamp"].str[:10] == datetime.now().strftime("%Y-%m-%d")]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Watcher", "On" if watcher.last_error is None else "Error")
    col2.metric("Notified", watcher.shown, help="Notifications shown since this app was started")
    col3.metric("Alerts today", len(today))
    last = log.iloc[0] if len(log) else None
    col4.metric("Last alert", "-" if last is None else last["timestamp"][11:],
                help=None if last is None else last["kind"])
    st.caption(f"Checking for new alerts every {CHECK_INTERVAL_SECONDS:.0f} s. Last check: "
               f"{watcher.last_check:%H:%M:%S}" if watcher.last_check else "Starting...")
    if watcher.last_error:
        st.error(f"Could not read the database: {watcher.last_error}")

    st.subheader("Notification log")
    if log.empty:
        st.info("No alerts yet. They appear here as soon as PulseVision raises one.",
                icon=":material/notifications_none:")
        return

    kinds = st.multiselect("Show", ALERT_KINDS, default=ALERT_KINDS, key="log_filter")
    shown = log[log["kind"].isin(kinds)].copy()
    shown["notify_status"] = shown["notify_status"].fillna("Waiting...")
    table = shown.rename(columns={
        "timestamp": "Time", "kind": "Alert", "condition": "Condition", "details": "Readings at that moment",
        "bpm": "Heart rate (BPM)", "quality": "Signal", "notify_status": "Notification",
        "session_id": "Session",
    })[["Time", "Alert", "Condition", "Readings at that moment", "Heart rate (BPM)", "Signal",
        "Notification", "Session"]]
    table["Time"] = pd.to_datetime(table["Time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"Heart rate (BPM)": st.column_config.NumberColumn(format="%.0f")})
    st.download_button("Download log (CSV)", table.to_csv(index=False), "pulsevision_notifications.csv",
                       "text/csv", icon=":material/download:")


live_view()
