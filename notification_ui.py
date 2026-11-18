"""Simple caregiver-facing notifications UI and historical analytics views."""

from datetime import date
import time

import pandas as pd
import streamlit as st

import database
from notifier import default_recipient, mask_email, missing_settings
from settings import METRICS, load_config, save_config

STATUS_COLORS = {
    "DELIVERED": "#35c48b", "SENT": "#4f8cff", "SENDING": "#4f8cff",
    "QUEUED": "#4f8cff", "RETRYING": "#f0ad4e", "DETECTED": "#f0ad4e",
    "FAILED": "#ff6b6b", "RECOVERED": "#9aa6b2", "TEST": "#b58cff",
    "SUPPRESSED": "#9aa6b2", "SKIPPED": "#9aa6b2",
}


def _inject_styles():
    st.markdown(
        """
        <style>
        .pv-card { background:var(--pv-card); border:1px solid var(--pv-border); border-radius:14px;
                   padding:16px 18px; min-height:92px; }
        .pv-label { color:var(--pv-muted); font-size:.72rem; font-weight:700; letter-spacing:.08em; }
        .pv-value { color:var(--pv-text); font-size:1.35rem; font-weight:700; margin-top:6px; }
        .pv-support { color:var(--pv-muted); font-size:.84rem; margin-top:4px; }
        .pv-status { border-radius:14px; padding:18px 20px; margin:4px 0 16px; }
        .pv-status-ok { background:var(--pv-status-ok-bg); border:1px solid var(--pv-status-ok-border); }
        .pv-status-warn { background:var(--pv-status-warn-bg); border:1px solid var(--pv-status-warn-border); }
        .pv-status-danger { background:var(--pv-status-danger-bg); border:1px solid var(--pv-status-danger-border); }
        .pv-status-muted { background:var(--pv-status-muted-bg); border:1px solid var(--pv-status-muted-border); }
        .pv-status-title { color:var(--pv-text); font-size:1.2rem; font-weight:700; }
        .pv-status-copy { color:var(--pv-muted); margin-top:4px; }
        .pv-activity { border-bottom:1px solid var(--pv-border); padding:12px 0; }
        .pv-activity-title { color:var(--pv-text); font-weight:650; }
        .pv-activity-copy { color:var(--pv-muted); font-size:.86rem; margin-top:3px; }
        .pv-badge { border-radius:999px; padding:3px 8px; font-size:.7rem; font-weight:700;
                    display:inline-block; margin-left:4px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _status_counts(frame):
    if frame.empty:
        return {"today": 0, "sent": 0, "delivered": 0, "pending": 0}
    real = ~frame["simulated"].astype(bool)
    timestamps = pd.to_datetime(frame["event_timestamp"], errors="coerce")
    statuses = frame["delivery_status"].astype(str).str.upper()
    return {
        "today": int(((timestamps.dt.date == date.today()) & real).sum()),
        "sent": int((real & statuses.isin(["SENT", "DELIVERED"])).sum()),
        "delivered": int((real & statuses.eq("DELIVERED")).sum()),
        "pending": int((real & statuses.isin(["DETECTED", "QUEUED", "SENDING", "RETRYING"])).sum()),
    }


def _system_state(service, notifier):
    missing = missing_settings()
    firebase_status = str(service.status).lower()
    if firebase_status.startswith("unavailable") or "failed" in firebase_status:
        return "Notification service unavailable", "danger", "A notification service needs attention."
    if missing:
        return "Configuration required", "warn", "Add the email settings before sending notifications."
    return "System ready", "ok", "Email notifications are configured and ready."


def _patient_state(active_alerts, monitoring):
    real_alerts = [alert for alert in (active_alerts or []) if not alert.simulated]
    if not monitoring:
        return "Monitoring unavailable", "muted", "Start monitoring to see the current patient status.", "◌"
    if not real_alerts:
        return "No active alerts", "ok", "No new conditions requiring attention.", "✓"
    if len(real_alerts) == 1:
        return "Attention required", "warn", "One active condition requires attention.", "!"
    return "Multiple active alerts", "danger", f"{len(real_alerts)} active conditions require attention.", "!"


def _display_time(value):
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_convert(None)
    return f"Today · {timestamp.strftime('%H:%M')}" if timestamp.date() == date.today() else timestamp.strftime("%d %b · %H:%M")


def _friendly_name(row):
    key = str(row.get("alert_key", ""))
    if "temp" in key:
        return "Temperature alert"
    if "motion" in key or "mpu" in key:
        return "Motion alert"
    if "absent" in key:
        return "Patient absence alert"
    if key == "email_test" or str(row.get("category", "")).upper() == "TEST":
        return "Test email"
    if "heart" in key or "hr_" in key:
        return "Heart-rate alert"
    return str(row.get("parameter") or row.get("category") or "Notification").title()


def _badge(label, color):
    return f'<span class="pv-badge" style="background:{color}22;color:{color}">{label}</span>'


def _render_email_test(email_notifier, config, frame):
    recipient = config.get("email", {}).get("recipient") or default_recipient()
    pending_id = st.session_state.get("pv_test_email_id")
    pending = False
    if pending_id and not frame.empty:
        match = frame[frame["notification_id"] == pending_id]
        pending = not match.empty and str(match.iloc[0]["delivery_status"]).upper() in {
            "QUEUED", "SENDING", "RETRYING",
        }
    if not pending and pending_id:
        st.session_state.pop("pv_test_email_id", None)

    action, help_column = st.columns([1, 2])
    with action:
        clicked = st.button("Send test email", type="primary", disabled=pending,
                            key="notification-test-email")
    with help_column:
        if pending:
            st.info("Your test email is being sent in the background…")
        else:
            st.caption("Sends one real test email only when you click the button.")

    if not clicked:
        return
    if not email_notifier:
        st.error("The email service is unavailable.")
        return
    if not recipient:
        st.error("Set a recipient email in Settings before sending a test email.")
        return

    from alerts import Alert

    now = time.time()
    test = Alert(key="email_test", category="TEST",
                 message="Explicit Gmail SMTP test requested from PulseVision.",
                 started_at=now, readings={}, simulated=True, notify=True)
    now_iso = pd.Timestamp.now(tz="UTC").isoformat()
    database.upsert_notification({
        "notification_id": test.uid, "alert_event_id": test.uid, "alert_key": test.key,
        "category": test.category, "patient_id": "Unassigned", "parameter": "Gmail SMTP",
        "trigger_reason": test.message, "measured_value": "Explicit test",
        "configured_threshold": "N/A", "event_timestamp": test.timestamp,
        "recipient_masked": mask_email(recipient), "channel": "EMAIL", "delivery_status": "QUEUED",
        "condition_status": "TEST", "provider": "Gmail SMTP", "provider_message_id": None,
        "attempt_count": 0, "last_attempt_at": now_iso, "delivered_at": None,
        "failure_reason": None, "simulated": 1, "updated_at": now_iso,
    }, event_detail="Explicit Send Test Email click", event_status="QUEUED")
    result = email_notifier.notify(
        test, recipient,
        on_done=lambda alert, status: database.update_notification_status(
            alert.uid, "SENT" if status == "sent" else "FAILED", detail=status,
            failure_reason=None if status == "sent" else status),
    )
    if result:
        database.update_notification_status(test.uid, "FAILED", detail=result, failure_reason=result)
        st.error(result)
    else:
        st.session_state.pv_test_email_id = test.uid
        st.success("Test email queued. Delivery status will appear in Recent activity.")


def _render_settings(service, config, frame):
    with st.expander("Settings", expanded=False):
        thresholds_tab, email_tab, advanced_tab = st.tabs(
            ["Alert thresholds", "Email notifications", "Advanced settings"])

        with thresholds_tab:
            st.caption("Choose only the conditions you want to monitor. Empty limits disable that alert.")
            with st.form("notification_alert_settings"):
                new_cfg = load_config()
                for key, meta in METRICS.items():
                    rule = new_cfg["thresholds"][key]
                    st.markdown(f"**{meta['label']}** ({meta['unit']})")
                    lo_valid, hi_valid = meta["valid_range"]
                    low_enabled, high_enabled = st.columns(2)
                    low_on = low_enabled.checkbox("Enable low alert", value=rule["low"] is not None,
                                                  key=f"notification-{key}-low-on")
                    high_on = high_enabled.checkbox("Enable high alert", value=rule["high"] is not None,
                                                    key=f"notification-{key}-high-on")
                    low_col, high_col = st.columns(2)
                    low_value = low_col.number_input("Low limit", min_value=float(lo_valid), max_value=float(hi_valid),
                                                    value=float(rule["low"] if rule["low"] is not None else lo_valid),
                                                    disabled=not low_on, key=f"notification-{key}-low")
                    high_value = high_col.number_input("High limit", min_value=float(lo_valid), max_value=float(hi_valid),
                                                      value=float(rule["high"] if rule["high"] is not None else hi_valid),
                                                      disabled=not high_on, key=f"notification-{key}-high")
                    rule["low"] = low_value if low_on else None
                    rule["high"] = high_value if high_on else None

                st.markdown("**Patient absence**")
                absence_on = st.checkbox("Alert when the patient is not detected", value=new_cfg["presence"]["absence_seconds"] is not None,
                                        key="notification-absence-on")
                absence_value = st.number_input("Absence duration (seconds)", min_value=1.0,
                                                value=float(new_cfg["presence"]["absence_seconds"] or 30),
                                                disabled=not absence_on, key="notification-absence-seconds")
                new_cfg["presence"]["absence_seconds"] = absence_value if absence_on else None
                st.info("Blood pressure monitoring is unavailable because no BP sensor is integrated.")
                disagreement_on = st.checkbox(
                    "Alert when sensor and camera heart rate differ", value=new_cfg["disagreement_bpm"] is not None,
                    key="notification-disagreement-on")
                disagreement = st.number_input("Heart-rate difference (BPM)", min_value=1.0,
                                               value=float(new_cfg["disagreement_bpm"] or 15), disabled=not disagreement_on,
                                               key="notification-disagreement")
                new_cfg["disagreement_bpm"] = disagreement if disagreement_on else None
                new_cfg["hardware_fault_alerts"] = st.checkbox(
                    "Alert on Arduino or sensor faults", value=new_cfg["hardware_fault_alerts"],
                    key="notification-hardware-faults")
                if st.form_submit_button("Save alert settings"):
                    errors = save_config(new_cfg)
                    if errors:
                        for error in errors:
                            st.error(error)
                    else:
                        config.clear()
                        config.update(new_cfg)
                        st.success("Alert settings saved.")

        with email_tab:
            st.markdown("#### Email notifications")
            recipient = config.get("email", {}).get("recipient") or default_recipient()
            st.caption(f"Configured recipient: **{mask_email(recipient)}**" if recipient else "No recipient configured")
            missing = missing_settings()
            if missing:
                st.warning("Email service needs configuration before it can send.")
            else:
                st.success("Email service ready")
            with st.form("notification_email_settings"):
                email_enabled = st.checkbox("Enable email alerts for real conditions",
                                            value=bool(config["email"].get("enabled")), key="notification-email-enabled")
                email_recipient = st.text_input("Recipient email", value=recipient, key="notification-email-recipient")
                if st.form_submit_button("Save email settings"):
                    candidate = dict(config)
                    candidate["email"] = {"enabled": email_enabled, "recipient": email_recipient.strip()}
                    errors = save_config(candidate)
                    if errors:
                        for error in errors:
                            st.error(error)
                    else:
                        config.clear()
                        config.update(candidate)
                        st.success("Email settings saved.")

        with advanced_tab:
            st.markdown("#### Delivery and duplicate prevention")
            st.caption("An alert is sent once when a condition starts. Confirmation, cooldown and recovery prevent duplicates.")
            st.checkbox("Show technical delivery details in activity", value=False,
                        key="notification_show_details")
            with st.form("notification_advanced_settings"):
                confirm = st.number_input("Confirmation time (seconds)", min_value=0.0,
                                          value=float(config.get("confirm_seconds", 3)), key="notification-confirm")
                cooldown = st.number_input("Cooldown for the same alert (seconds)", min_value=0.0,
                                           value=float(config.get("cooldown_seconds", 300)), key="notification-cooldown")
                stale = st.number_input("Arduino stale timeout (seconds)", min_value=1.0,
                                        value=float(config["serial"].get("stale_seconds", 5)), key="notification-stale")
                if st.form_submit_button("Save advanced settings"):
                    candidate = dict(config)
                    candidate["confirm_seconds"], candidate["cooldown_seconds"] = confirm, cooldown
                    candidate["serial"] = dict(config["serial"], stale_seconds=stale)
                    errors = save_config(candidate)
                    if errors:
                        for error in errors:
                            st.error(error)
                    else:
                        config.clear()
                        config.update(candidate)
                        st.success("Advanced settings saved.")

            st.markdown("#### Activity filters")
            categories = sorted(frame["category"].dropna().astype(str).unique()) if not frame.empty else []
            statuses = sorted(frame["delivery_status"].dropna().astype(str).str.upper().unique()) if not frame.empty else []
            channels = sorted(frame["channel"].dropna().astype(str).str.upper().unique()) if not frame.empty else []
            patients = sorted(frame["patient_id"].dropna().astype(str).unique()) if not frame.empty else []
            st.multiselect("Alert category", categories, key="notification_category_filter")
            st.multiselect("Delivery status", statuses, key="notification_status_filter")
            st.multiselect("Channel", channels, key="notification_channel_filter")
            st.multiselect("Patient identifier", patients, key="notification_patient_filter")
            d1, d2 = st.columns(2)
            d1.date_input("From", value=None, key="notification_date_from")
            d2.date_input("To", value=None, key="notification_date_to")
            st.text_input("Search alert events", key="notification_search")

            st.markdown("#### Firebase and push notifications")
            st.caption(f"Service status: {service.status}")
            cfg = st.session_state.get("pv_notification_form", {})
            with st.form("firebase_notification_config"):
                patient_id = st.text_input("Patient identifier", value=cfg.get("patient_id", "Unassigned"))
                phone = st.text_input("Recipient phone", value="", type="password")
                channel = st.selectbox("Delivery channel", ["push", "sms", "both"],
                                       index=["push", "sms", "both"].index(cfg.get("channel", "push")))
                fcm_token = st.text_input("FCM device token", value="", type="password")
                msg91_flow = st.text_input("MSG91 flow/template ID", value="")
                msg91_sender = st.text_input("MSG91 sender ID", value="")
                enabled = st.checkbox("Enable Firebase notifications", value=bool(cfg.get("enabled", False)))
                if st.form_submit_button("Save Firebase settings"):
                    settings = {"patient_id": patient_id.strip(), "phone": phone.strip(), "channel": channel,
                                "fcm_token": fcm_token.strip(), "msg91_flow_id": msg91_flow.strip(),
                                "msg91_sender_id": msg91_sender.strip(), "enabled": enabled}
                    try:
                        service.save_patient_config(settings)
                        config["notifications"].update({"enabled": enabled, "patient_id": settings["patient_id"], "channel": channel})
                        errors = save_config(config)
                        if errors:
                            raise ValueError("; ".join(errors))
                        st.session_state.pv_notification_form = {"patient_id": settings["patient_id"], "channel": channel, "enabled": enabled}
                        st.success("Firebase settings saved.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not save Firebase settings: {type(exc).__name__}: {exc}")

            if st.button("Create local test temperature event", key="notification-test-temperature"):
                from alerts import Alert
                from monitor import Snapshot

                now = time.time()
                test = Alert(key="hw_object_temp_c_high", category="TEST",
                             message="TEST ONLY: simulated skin surface temperature threshold event.",
                             started_at=now, readings={"Skin surface temperature": "TEST sample"},
                             session_id=None, simulated=True, notify=False)
                service.record_alert(test, config, Snapshot(time=now, timestamp=pd.Timestamp.now().isoformat(timespec="seconds")))
                st.success("Local test event recorded. No email or provider request was made.")


def _render_activity(service, frame):
    st.markdown("#### Recent activity")
    if frame.empty:
        st.info("No recent notifications.")
        return

    view = frame.copy()
    category_filter = st.session_state.get("notification_category_filter", [])
    status_filter = st.session_state.get("notification_status_filter", [])
    channel_filter = st.session_state.get("notification_channel_filter", [])
    patient_filter = st.session_state.get("notification_patient_filter", [])
    view["_time"] = pd.to_datetime(view["event_timestamp"], errors="coerce")
    if category_filter:
        view = view[view["category"].isin(category_filter)]
    if status_filter:
        view = view[view["delivery_status"].astype(str).str.upper().isin(status_filter)]
    if channel_filter:
        view = view[view["channel"].astype(str).str.upper().isin(channel_filter)]
    if patient_filter:
        view = view[view["patient_id"].isin(patient_filter)]
    start_date = st.session_state.get("notification_date_from")
    end_date = st.session_state.get("notification_date_to")
    if start_date:
        view = view[view["_time"].dt.date >= start_date]
    if end_date:
        view = view[view["_time"].dt.date <= end_date]
    search = str(st.session_state.get("notification_search", "")).strip().lower()
    if search:
        text = view[["alert_key", "parameter", "trigger_reason", "patient_id"]].fillna("").astype(str).agg(" ".join, axis=1)
        view = view[text.str.lower().str.contains(search, regex=False)]
    if view.empty:
        st.info("No recent notifications.")
        return

    for _, row in view.sort_values("event_timestamp", ascending=False).head(12).iterrows():
        status = str(row["delivery_status"]).upper()
        event_type = "TEST" if bool(row["simulated"]) else "REAL"
        status_color = STATUS_COLORS.get(status, "#9aa6b2")
        event_color = STATUS_COLORS["TEST"] if event_type == "TEST" else "#35c48b"
        description = str(row["trigger_reason"] or row["measured_value"] or "Notification event")
        if len(description) > 115:
            description = description[:112] + "…"
        left, right = st.columns([4, 1])
        with left:
            st.markdown(
                f'<div class="pv-activity"><div class="pv-activity-title">{_friendly_name(row)} '
                f'{_badge(event_type, event_color)} {_badge(status, status_color)}</div>'
                f'<div class="pv-activity-copy">{description}</div>'
                f'<div class="pv-activity-copy">{_display_time(row["event_timestamp"])} · '
                f'{"Recipient notified" if status in {"SENT", "DELIVERED"} else "Notification status: " + status.title()}</div></div>',
                unsafe_allow_html=True,
            )
        with right:
            if status == "FAILED" and not bool(row["simulated"]):
                if st.button("Retry", key=f"retry-{row['notification_id']}"):
                    try:
                        service.retry_notification(row["notification_id"])
                        st.success("Retry queued.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Retry failed: {type(exc).__name__}: {exc}")
        if st.session_state.get("notification_show_details", False):
            st.caption(f"Condition: {row['condition_status']} · Channel: {row['channel']} · Recipient: {row['recipient_masked']}")
            if row["failure_reason"]:
                st.error(str(row["failure_reason"]))


def render_notification_feed(service, config, email_notifier=None, active_alerts=None, monitoring=False):
    """Render a caregiver-facing notification page without changing delivery behavior."""
    _inject_styles()
    frame = database.load_notifications(limit=500)
    system_text, system_kind, _ = _system_state(service, email_notifier)
    patient_text, patient_kind, patient_copy, patient_icon = _patient_state(active_alerts, monitoring)

    st.markdown("## Notifications & Alerts")
    system_color = "#35c48b" if system_kind == "ok" else "#f0ad4e" if system_kind == "warn" else "#ff6b6b"
    st.markdown(f'<div style="text-align:right;color:#b6c1cc;margin-top:-42px;margin-bottom:18px"><span style="color:{system_color}">●</span> {system_text}</div>', unsafe_allow_html=True)
    status_class = {"ok": "pv-status-ok", "warn": "pv-status-warn", "danger": "pv-status-danger", "muted": "pv-status-muted"}[patient_kind]
    st.markdown(f'<div class="pv-status {status_class}"><div class="pv-label">PATIENT STATUS</div><div class="pv-status-title">{patient_icon} {patient_text}</div><div class="pv-status-copy">{patient_copy}</div></div>', unsafe_allow_html=True)

    _render_email_test(email_notifier, config, frame)
    frame = database.load_notifications(limit=500)
    counts = _status_counts(frame)
    metric_one, metric_two = st.columns(2)
    with metric_one:
        st.markdown(f'<div class="pv-card"><div class="pv-label">TOTAL ALERTS TODAY</div><div class="pv-value">{counts["today"]}</div><div class="pv-support">Real patient alerts</div></div>', unsafe_allow_html=True)
    with metric_two:
        st.markdown(f'<div class="pv-card"><div class="pv-label">EMAILS SENT</div><div class="pv-value">{counts["sent"]}</div><div class="pv-support">{counts["delivered"]} delivery confirmations</div></div>', unsafe_allow_html=True)

    _render_settings(service, config, frame)
    _render_activity(service, frame)


def render_analytics():
    """Compact first-pass trends sourced only from persisted measurements."""
    frame = database.load_monitor_log(limit=3000)
    if frame.empty:
        st.info("Analytics will appear after monitoring records are saved.")
        return
    st.caption("Historical trends use recorded measurements only; missing values remain gaps.")
    for title, columns in (("Heart rate", ["hw_hr_bpm", "cam_bpm"]), ("SpO₂", ["hw_spo2_pct"]),
                           ("Motion", ["hw_motion_g"]), ("Skin surface temperature", ["hw_object_temp_c"])):
        available = [column for column in columns if column in frame and frame[column].notna().any()]
        if not available:
            continue
        st.markdown(f"#### {title}")
        chart = frame.set_index(pd.to_datetime(frame["timestamp"], errors="coerce"))[available]
        st.line_chart(chart, height=180)
    st.dataframe(frame.tail(100), hide_index=True, use_container_width=True)
