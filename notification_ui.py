"""Notification feed and lightweight historical analytics views for Streamlit."""

from datetime import date
import time

import pandas as pd
import streamlit as st

import database
from settings import save_config

STATUS_COLORS = {
    "DELIVERED": "#16803c", "SENT": "#2563eb", "SENDING": "#2563eb",
    "QUEUED": "#2563eb", "RETRYING": "#d97706", "DETECTED": "#d97706",
    "FAILED": "#dc2626", "RECOVERED": "#6b7280", "TEST": "#7c3aed",
    "SUPPRESSED": "#6b7280", "SKIPPED": "#6b7280",
}


def _status_counts(frame):
    statuses = frame["delivery_status"].astype(str).str.upper() if not frame.empty else pd.Series(dtype=str)
    return {
        "today": int((pd.to_datetime(frame["event_timestamp"], errors="coerce").dt.date == date.today()).sum()) if not frame.empty else 0,
        "sent": int(statuses.eq("SENT").sum()),
        "delivered": int(statuses.eq("DELIVERED").sum()),
        "failed": int(statuses.eq("FAILED").sum()),
        "pending": int(statuses.isin(["DETECTED", "QUEUED", "SENDING", "RETRYING"]).sum()),
    }


def render_notification_feed(service, config):
    """Render a refreshable feed; Firebase polling and provider work stay off this thread."""
    frame = database.load_notifications(limit=500)
    counts = _status_counts(frame)
    summary = st.columns(5)
    for column, label, key in zip(summary,
                                  ("Total alerts today", "Sent (provider accepted)", "Delivered", "Failed", "Pending"),
                                  ("today", "sent", "delivered", "failed", "pending")):
        column.metric(label, counts[key])

    if st.button("Create TEST temperature alert (no SMS or push)", key="notification-test-temperature"):
        from alerts import Alert
        from monitor import Snapshot

        now = time.time()
        test = Alert(
            key="hw_object_temp_c_high", category="TEST",
            message="TEST ONLY: simulated skin surface temperature threshold event.",
            started_at=now, readings={"Skin surface temperature": "TEST sample: 38.0 °C"},
            session_id=None, simulated=True, notify=False,
        )
        snap = Snapshot(time=now, timestamp=pd.Timestamp.now().isoformat(timespec="seconds"))
        service.record_alert(test, config, snap)
        st.success("TEST event recorded locally. No provider request was made.")

    with st.expander("Patient notification settings", expanded=False):
        st.caption("Configuration is stored in Firebase by the server. SMS credentials belong in Cloud Functions secrets.")
        cfg = st.session_state.get("pv_notification_form", {})
        with st.form("firebase_notification_config"):
            patient_id = st.text_input("Patient identifier", value=cfg.get("patient_id", "Unassigned"))
            phone = st.text_input("Recipient phone", value="", type="password",
                                  help="Phone number is sent only to the trusted server and is masked in the feed.")
            channel = st.selectbox("Delivery channel", ["push", "sms", "both"],
                                   index=["push", "sms", "both"].index(cfg.get("channel", "push")))
            fcm_token = st.text_input("FCM device token", value="", type="password")
            msg91_flow = st.text_input("MSG91 flow/template ID", value="")
            msg91_sender = st.text_input("MSG91 sender ID", value="")
            enabled = st.checkbox("Enable Firebase notifications", value=bool(cfg.get("enabled", False)))
            submitted = st.form_submit_button("Save Firebase settings")
        if submitted:
            settings = {
                "patient_id": patient_id.strip(), "phone": phone.strip(), "channel": channel,
                "fcm_token": fcm_token.strip(), "msg91_flow_id": msg91_flow.strip(),
                "msg91_sender_id": msg91_sender.strip(), "enabled": enabled,
            }
            try:
                service.save_patient_config(settings)
                config["notifications"].update({
                    "enabled": enabled, "patient_id": settings["patient_id"], "channel": channel,
                })
                errors = save_config(config)
                if errors:
                    raise ValueError("; ".join(errors))
                st.session_state.pv_notification_form = {
                    "patient_id": settings["patient_id"], "channel": channel, "enabled": enabled,
                }
                st.success("Firebase settings saved. Recipient: " + service.mask_phone(phone))
            except Exception as exc:  # noqa: BLE001 - surface setup errors without disrupting monitoring
                st.error(f"Could not save Firebase settings: {type(exc).__name__}: {exc}")

    if frame.empty:
        st.info("No alert notifications recorded yet. Live alert events will appear here automatically.")
        return

    categories = sorted(frame["category"].dropna().astype(str).unique().tolist())
    statuses = sorted(frame["delivery_status"].dropna().astype(str).str.upper().unique().tolist())
    channels = sorted(frame["channel"].dropna().astype(str).str.upper().unique().tolist())
    patients = sorted(frame["patient_id"].dropna().astype(str).unique().tolist())
    f1, f2, f3, f4 = st.columns(4)
    category_filter = f1.multiselect("Alert category", categories, key="notification_category_filter")
    status_filter = f2.multiselect("Delivery status", statuses, key="notification_status_filter")
    channel_filter = f3.multiselect("Channel", channels, key="notification_channel_filter")
    patient_filter = f4.multiselect("Patient identifier", patients, key="notification_patient_filter")
    d1, d2 = st.columns(2)
    start_date = d1.date_input("From", value=None, key="notification_date_from")
    end_date = d2.date_input("To", value=None, key="notification_date_to")
    search = st.text_input("Search alert events", key="notification_search").strip().lower()

    view = frame.copy()
    view["_time"] = pd.to_datetime(view["event_timestamp"], errors="coerce")
    if category_filter:
        view = view[view["category"].isin(category_filter)]
    if status_filter:
        view = view[view["delivery_status"].astype(str).str.upper().isin(status_filter)]
    if channel_filter:
        view = view[view["channel"].astype(str).str.upper().isin(channel_filter)]
    if patient_filter:
        view = view[view["patient_id"].isin(patient_filter)]
    if start_date:
        view = view[view["_time"].dt.date >= start_date]
    if end_date:
        view = view[view["_time"].dt.date <= end_date]
    if search:
        text_columns = ["alert_key", "parameter", "measured_value", "failure_reason", "patient_id"]
        matches = view[text_columns].fillna("").astype(str).agg(" ".join, axis=1).str.lower().str.contains(search, regex=False)
        view = view[matches]

    st.caption(f"Firebase sync: {service.status} · {len(view)} matching events · updates every 2 seconds")
    if view.empty:
        st.info("No alert events match these filters.")
        return

    for _, row in view.sort_values("event_timestamp", ascending=False).iterrows():
        status = str(row["delivery_status"]).upper()
        color = STATUS_COLORS.get(status, "#6b7280")
        test_tag = " · TEST" if bool(row["simulated"]) else ""
        title = (f"{row['event_timestamp']} · {row['category']} · {row['parameter']} · "
                 f"{status}{test_tag}")
        with st.expander(title):
            st.markdown(f"<span style='color:{color};font-weight:700'>{status}</span>", unsafe_allow_html=True)
            st.write(f"**Patient:** {row['patient_id']}  ·  **Value:** {row['measured_value']}  ·  **Threshold:** {row['configured_threshold']}")
            st.write(f"**Trigger reason:** {row['trigger_reason']}")
            st.write(f"**Recipient:** {row['recipient_masked']}  ·  **Channel:** {row['channel']}  ·  **Provider:** {row['provider']}")
            st.write(f"**Notification ID:** `{row['notification_id']}`  ·  **Alert event ID:** `{row['alert_event_id']}`")
            st.write(f"**Condition:** {row['condition_status']}  ·  **Attempts:** {row['attempt_count']}  ·  **Last attempt:** {row['last_attempt_at'] or '—'}")
            st.write(f"**Provider reference:** {row['provider_message_id'] or '—'}  ·  **Delivery confirmed:** {row['delivered_at'] or '—'}")
            if row["failure_reason"]:
                st.error(str(row["failure_reason"]))
            history = database.load_notification_events(row["notification_id"])
            if not history.empty:
                st.dataframe(history, hide_index=True, use_container_width=True)
            if status == "FAILED" and not bool(row["simulated"]):
                if st.button("Retry failed notification", key=f"retry-{row['notification_id']}"):
                    try:
                        service.retry_notification(row["notification_id"])
                        st.success("Retry queued. The existing notification ID is retained to prevent duplicates.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Retry was not queued: {type(exc).__name__}: {exc}")


def render_analytics():
    """Compact first-pass trends sourced only from persisted measurements."""
    frame = database.load_monitor_log(limit=3000)
    if frame.empty:
        st.info("Analytics will appear after monitoring records are saved.")
        return
    st.caption("Historical trends use recorded measurements only; missing values remain gaps.")
    for title, columns in (
        ("Heart rate", ["hw_hr_bpm", "cam_bpm"]),
        ("SpO₂", ["hw_spo2_pct"]),
        ("Motion", ["hw_motion_g"]),
        ("Skin surface temperature", ["hw_object_temp_c"]),
    ):
        available = [column for column in columns if column in frame and frame[column].notna().any()]
        if not available:
            continue
        st.markdown(f"#### {title}")
        chart = frame.set_index(pd.to_datetime(frame["timestamp"], errors="coerce"))[available]
        st.line_chart(chart, height=180)
    st.dataframe(frame.tail(100), hide_index=True, use_container_width=True)
