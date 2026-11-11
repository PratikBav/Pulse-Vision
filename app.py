"""PulseVision - Streamlit dashboard.

Phase 1: webcam preview, face detection, facial landmarks and forehead ROI.
Phase 2: mean RGB extraction from the forehead, rolling 12 s buffer, live RGB plots.
Phase 3: POS rPPG pulse waveform.
Phase 4: band-pass filter, FFT, BPM, smoothing and signal quality.
Storage: readings are kept per session and saved to SQLite with "Save session".
Hardware: Arduino sensors over USB serial, bed presence, alert rules, email alerts
and a once-per-second monitoring log (hardware.py, monitor.py, alerts.py, notifier.py).
Run with:  streamlit run app.py
"""

import logging
import os
import time
from datetime import datetime

import streamlit as st

from alerts import Alert, AlertEngine
from camera import Camera
from database import save_readings
from face import FaceMeshDetector, draw_landmarks
from firebase_notifications import FirebaseNotificationService
from hardware import MOCK_PORT, SerialReader, detect_arduino_port, list_serial_ports
from monitor import DataRecorder, PresenceTracker, build_snapshot
from notifier import EmailNotifier, missing_settings
from notification_ui import render_analytics, render_notification_feed
from roi import draw_roi, extract_roi_pixels, get_forehead_roi, mean_rgb
from rppg import pos
from settings import METRICS, load_config, save_config
from signal_processing import (MAX_BPM, MIN_BPM, SAMPLE_RATE, BPMSmoother, RGBBuffer,
                               bandpass_filter, find_heart_rate, power_spectrum, resample,
                               signal_quality)

PROCESS_INTERVAL_SECONDS = 1.0  # update heart rate and charts once per second
MIN_PULSE_SECONDS = 3.0         # need a few seconds of signal before showing the waveform
MIN_BPM_SECONDS = 10.0          # need several heartbeats before estimating the heart rate
MONITOR_INTERVAL_SECONDS = 1.0  # combine hardware + camera, check alerts and log once per second
POOR_SIGNAL_MESSAGE = "Signal quality is poor. Please remain still and improve lighting."
LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "pulsevision.log")


# ---- Process-wide singletons: they survive Streamlit reruns, so the serial port is
# ---- opened once and alert cooldowns are not forgotten when a widget is clicked.
@st.cache_resource
def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("pulsevision")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


@st.cache_resource
def get_reader():
    """The one SerialReader. Connects automatically to a detected Arduino on first start."""
    reader = SerialReader()
    cfg = load_config()
    port = cfg["serial"]["port"]
    if port == "auto":
        port = detect_arduino_port()
    if port and port != MOCK_PORT and port in [p for p, _ in list_serial_ports()]:
        reader.connect(port, cfg["serial"]["baud"])
    return reader


@st.cache_resource
def get_engine():
    return AlertEngine()


@st.cache_resource
def get_recorder():
    return DataRecorder()


@st.cache_resource
def get_notifier():
    return EmailNotifier()


@st.cache_resource
def get_firebase_notification_service():
    service = FirebaseNotificationService()
    service.start_background_sync(interval_seconds=2.0)
    return service


def show_session_status(placeholder):
    """Show how many readings the current session has, and how many are not saved yet."""
    session_id = st.session_state.get("session_id")
    if session_id is None:
        placeholder.caption("No session yet - start a measurement.")
        return
    total = len(st.session_state.readings)
    unsaved = total - st.session_state.saved_count
    placeholder.caption(f"Session {session_id}: {total} readings, {unsaved} not saved")


def _num(value):
    """Optional number as float (Streamlit number inputs must not mix int and float)."""
    return None if value is None else float(value)


def render_monitor_panel(snap, active_alerts, monitoring, record_enabled, recorder_status):
    """Hardware readings, patient presence, active alerts and recording status."""
    st.markdown("##### Arduino sensors")
    icons = {"connected": "🟢", "connecting": "🟡", "reconnecting": "🟠", "error": "🔴", "disconnected": "⚪"}
    conn_line = f"{icons.get(snap.hw_status, '⚪')} Connection: **{snap.hw_status}**"
    if snap.hw_port:
        conn_line += f" ({snap.hw_port})"
    if snap.hw_status == "connected":
        freshness = "⚠️ stale (waiting for new line)" if snap.hw_stale else "fresh (1 Hz)"
        conn_line += f" | Data: **{freshness}**"
    elif snap.hw_last_received is not None and snap.hw_stale:
        conn_line += f" | Last data: **stale ({snap.hw_age_s:.0f} s ago)**"
    st.caption(conn_line)
    if snap.hw_simulated:
        st.warning("SIMULATED readings (mock serial) - not real patient measurements.")
    if snap.hw_format == "legacy_raw":
        st.info("Arduino is sending raw IR/RED values only (old sketch). Upload "
                "firmware/pulsevision_sensors to get heart rate and SpO2.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sensor HR", f"{snap.hw_hr_bpm:.0f} BPM" if snap.hw_hr_bpm is not None else "--")
    c2.metric("Sensor SpO2*", f"{snap.hw_spo2_pct:.0f} %" if snap.hw_spo2_pct is not None else "--")
    c3.metric("Motion (MPU)", f"{snap.hw_motion_g:.2f} g" if snap.hw_motion_g is not None else "--")
    temp_val = f"{snap.hw_object_temp_c:.1f} °C" if snap.hw_object_temp_c is not None else "--"
    temp_secondary = f"Ambient: {snap.hw_ambient_temp_c:.1f} °C" if snap.hw_ambient_temp_c is not None else None
    if snap.hw_mlx_ok is False:
        temp_val = "Error"
        temp_secondary = "Sensor fault"
    c4.metric(
        "Skin Surface Temperature",
        temp_val,
        delta=temp_secondary,
        delta_color="off",
    )
    if snap.hw_last_received is not None:
        last = datetime.fromtimestamp(snap.hw_last_received).strftime("%H:%M:%S")
        mpu_txt = ""
        if snap.hw_accel_g is not None:
            mpu_txt = f" | Accel: {snap.hw_accel_g:.2f} g"
            if snap.hw_gyro_dps is not None:
                mpu_txt += f", Gyro: {snap.hw_gyro_dps:.0f} °/s"
        temp_txt = ""
        if snap.hw_ambient_temp_c is not None:
            temp_txt = f" | Amb: {snap.hw_ambient_temp_c:.1f} °C"
        st.caption(f"Last reading {last} ({snap.hw_age_s:.0f} s ago){mpu_txt}{temp_txt}. *SpO2 is uncalibrated. Skin surface temp is NOT core body temp.")
    if snap.hw_sensor_ok is False:
        st.error("MAX30100 reports a sensor error - check wiring.")
    if snap.hw_mpu_ok is False:
        st.error("MPU9250/6500/9255 reports a sensor error - check wiring.")
    if snap.hw_mlx_ok is False:
        st.error("MLX90614 temperature sensor reports an error - check wiring.")

    st.markdown("##### Patient and alerts")
    if not monitoring:
        st.caption("Monitoring is off - alerts are not being checked.")
        return
    presence_text = {"present": "In view", "absent": f"Not detected ({snap.absent_seconds:.0f} s)",
                     "unknown": "Unknown (camera unavailable)"}[snap.presence]
    st.metric("Patient presence", presence_text)
    if active_alerts:
        for alert in active_alerts:
            st.error(f"**{alert.category}**: {alert.message}  \nEmail: {alert.delivery_status}")
    else:
        st.success("No active alerts")
    if record_enabled:
        text = f"Recording: {recorder_status['rows_written']} rows saved"
        if recorder_status["last_error"]:
            st.warning(f"{text} - {recorder_status['last_error']}")
        else:
            st.caption(text + " to data/pulsevision.db")
    else:
        st.caption("Recording is off.")


st.set_page_config(page_title="PulseVision", layout="wide")
setup_logging()
reader = get_reader()
engine = get_engine()
recorder = get_recorder()
notifier = get_notifier()
firebase_notifications = get_firebase_notification_service()
config = load_config()

st.title("PulseVision")
st.caption("Computer Vision Based Non Contact Heart Rate Detection Using Facial Video")

# ---- Sidebar: Arduino connection and alert settings ----
with st.sidebar:
    st.header("Arduino")
    ports = list_serial_ports()
    port_names = [p for p, _ in ports] + [MOCK_PORT]
    descriptions = dict(ports)
    hw_state = reader.snapshot()
    preferred = hw_state["port"] or config["serial"]["port"]
    if preferred not in port_names:
        preferred = detect_arduino_port() or port_names[0]
    port = st.selectbox("Serial port", port_names, index=port_names.index(preferred),
                        format_func=lambda p: f"{p} - {descriptions[p]}" if p in descriptions else p)
    baud = st.number_input("Baud rate", min_value=300, max_value=2_000_000,
                           value=int(config["serial"]["baud"]), step=100)
    b1, b2, b3 = st.columns(3)
    if b1.button("Connect", width="stretch"):
        reader.connect(port, baud)
        config["serial"]["port"], config["serial"]["baud"] = port, int(baud)
        save_config(config)
        time.sleep(0.5)
    if b2.button("Disconnect", width="stretch"):
        reader.disconnect()
    b3.button("Refresh", width="stretch", help="Refresh the list of serial ports")
    hw_state = reader.snapshot()
    st.caption(f"Connection: **{hw_state['status']}**" + (f" on {hw_state['port']}" if hw_state["port"] else ""))
    if hw_state["last_error"] and hw_state["status"] != "connected":
        st.caption(f"Last error: {hw_state['last_error']}")
    if hw_state["status"] != "disconnected":
        st.caption(f"Valid readings: {hw_state['valid_count']} | rejected lines: {hw_state['malformed_count']}"
                   + (f" | reconnects: {hw_state['reconnect_count']}" if hw_state.get("reconnect_count") else ""))

    record_enabled = st.toggle("Record monitoring data", value=True,
                               help="Save hardware, camera, presence and alert data once per second")

    with st.expander("Alert settings"):
        with st.form("alert_settings"):
            st.caption("Leave a threshold empty to disable it. No clinical defaults are provided.")
            new_cfg = load_config()
            for key, meta in METRICS.items():
                st.markdown(f"**{meta['label']}** ({meta['unit']})")
                lo_valid, hi_valid = meta["valid_range"]
                c_low, c_high = st.columns(2)
                rule = new_cfg["thresholds"][key]
                rule["low"] = c_low.number_input("Low", value=_num(rule["low"]), min_value=float(lo_valid),
                                                 max_value=float(hi_valid), key=f"{key}_low")
                rule["high"] = c_high.number_input("High", value=_num(rule["high"]), min_value=float(lo_valid),
                                                   max_value=float(hi_valid), key=f"{key}_high")
            new_cfg["presence"]["absence_seconds"] = st.number_input(
                "Alert when patient not detected for (s)", value=_num(new_cfg["presence"]["absence_seconds"]),
                min_value=1.0)
            new_cfg["disagreement_bpm"] = st.number_input(
                "Alert when sensor and camera HR differ by more than (BPM)",
                value=_num(new_cfg["disagreement_bpm"]), min_value=1.0)
            new_cfg["hardware_fault_alerts"] = st.checkbox(
                "Alert on Arduino disconnection / stale data", value=new_cfg["hardware_fault_alerts"])
            new_cfg["serial"]["stale_seconds"] = st.number_input(
                "Treat Arduino data as stale after (s)", value=float(new_cfg["serial"]["stale_seconds"]),
                min_value=1.0)
            new_cfg["confirm_seconds"] = st.number_input(
                "Condition must last (s) before alerting", value=float(new_cfg["confirm_seconds"]),
                min_value=0.0)
            new_cfg["cooldown_seconds"] = st.number_input(
                "Cooldown between alerts of the same kind (s)", value=float(new_cfg["cooldown_seconds"]),
                min_value=0.0)
            new_cfg["email"]["enabled"] = st.checkbox("Send email alerts", value=new_cfg["email"]["enabled"])
            new_cfg["email"]["recipient"] = st.text_input("Recipient email", value=new_cfg["email"]["recipient"])
            if st.form_submit_button("Save settings"):
                errors = save_config(new_cfg)
                if errors:
                    for error in errors:
                        st.error(error)
                else:
                    config = new_cfg
                    st.success("Settings saved.")

        missing = missing_settings()
        if missing:
            st.caption("Email not configured - set " + ", ".join(missing) + " (see .env.example).")
        if st.button("Send test email"):
            test = Alert(key="test", category="TEST", message="Test email from PulseVision.",
                         started_at=time.time(), readings={})
            result = notifier.notify(test, config["email"]["recipient"])
            st.info(result or "Sending in the background - click Refresh to see the result.")
        if notifier.last_result:
            st.caption(f"Last email ({notifier.last_result[0]}): {notifier.last_result[1]}")

tab_live, tab_analytics, tab_notifications = st.tabs(
    ["Live Monitoring", "Analytics", "Notifications & Alerts"])
with tab_analytics:
    render_analytics()
with tab_notifications:
    @st.fragment(run_every="2s")
    def live_notification_feed():
        render_notification_feed(firebase_notifications, config)
    live_notification_feed()

with tab_live:
    col_video, col_info = st.columns([2, 1])

    with col_info:
        running = st.toggle("Start measurement")
        show_landmarks = st.checkbox("Show facial landmarks")

        # st.session_state keeps values between Streamlit reruns (e.g. when a button is
        # clicked), so the readings are not lost. Switching measurement on starts a new session.
        if running and not st.session_state.get("was_running", False):
            st.session_state.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            st.session_state.readings = []
            st.session_state.saved_count = 0
            engine.reset()
        st.session_state.was_running = running

        save_clicked = st.button("Save session")
        save_message = st.empty()
        session_status = st.empty()
        if save_clicked:
            new_readings = st.session_state.get("readings", [])[st.session_state.get("saved_count", 0):]
            if not new_readings:
                save_message.info("No new readings to save yet.")
            else:
                save_readings(st.session_state.session_id, new_readings)
                st.session_state.saved_count += len(new_readings)
                save_message.success(f"Saved {len(new_readings)} readings to the database.")
        show_session_status(session_status)

        heart_rate_metric = st.empty()
        quality_metric = st.empty()
        face_status = st.empty()
        signal_status = st.empty()
        signal_progress = st.empty()
        st.divider()
        monitor_panel = st.empty()

    with col_video:
        video_placeholder = st.empty()

    @st.fragment(run_every=1.0)
    def idle_monitor_panel():
        """Live Arduino status while measurement is off (alerts are not checked)."""
        snap = build_snapshot(reader.snapshot(), config["serial"]["stale_seconds"])
        render_monitor_panel(snap, [], False, record_enabled, recorder.status())


    if not running:
        video_placeholder.info("Turn on 'Start measurement' to open the webcam.")
        heart_rate_metric.metric("Estimated Heart Rate", "--")
        with monitor_panel.container():
            idle_monitor_panel()
        st.stop()


    def on_email_done(alert, status):
        recorder.update_alert(alert.uid, delivery_status=status)


    def monitor_step(camera_status, face_detected, presence, absent_seconds, cam_bpm, cam_quality):
        """Combine hardware + camera, evaluate alert rules, notify, record and update the panel."""
        hw = reader.snapshot()
        snap = build_snapshot(
            hw, config["serial"]["stale_seconds"], session_id=st.session_state.session_id,
            camera_status=camera_status, cam_bpm=cam_bpm, cam_quality=cam_quality,
            face_detected=face_detected, presence=presence, absent_seconds=absent_seconds,
        )
        new_alerts, cleared = engine.evaluate(snap, config, hardware_expected=hw["status"] != "disconnected")
        snap.active_alerts = engine.active_keys()
        for alert in new_alerts:
            recorder.record_alert(alert)
            firebase_notifications.record_alert(alert, config, snap)
            if not alert.notify:
                continue
            if config["email"]["enabled"]:
                notifier.notify(alert, config["email"]["recipient"], on_done=on_email_done)
            else:
                alert.delivery_status = "not sent (email alerts disabled)"
                recorder.update_alert(alert.uid, delivery_status=alert.delivery_status)
        for alert in cleared:
            recorder.update_alert(alert.uid, cleared_at=snap.timestamp)
            firebase_notifications.mark_recovered(alert)
        if record_enabled:
            recorder.log_snapshot(snap)
        with monitor_panel.container():
            render_monitor_panel(snap, list(engine.active.values()), True, record_enabled, recorder.status())


    camera = Camera()
    if not camera.is_opened():
        camera.release()
        camera = None
        # Hardware monitoring and alerts keep running without the camera
        video_placeholder.error(
            "Could not open the webcam. Check that it is connected and not already "
            "in use by another app or another PulseVision browser tab. "
            "Arduino monitoring continues without the camera."
        )

    detector = None
    buffer = RGBBuffer()
    smoother = BPMSmoother()
    presence_tracker = PresenceTracker()

    # Streamlit stops this loop automatically when the toggle is switched off,
    # and the 'finally' block releases the webcam.
    try:
        if camera is not None:
            detector = FaceMeshDetector()
        start_time = time.time()
        last_process_time = 0.0
        last_monitor_time = 0.0
        heart_rate = None  # smoothed BPM shown to the user, None when not available
        quality = None     # "Good" / "Fair" / "Poor", None while collecting signal
        while True:
            frame = camera.read() if camera is not None else None
            if frame is None:
                if camera is not None:
                    video_placeholder.error("Could not read a frame from the webcam. "
                                            "Arduino monitoring continues without the camera.")
                    camera.release()
                    camera = None
                    buffer.clear()
                    smoother.clear()
                    heart_rate = None
                    quality = None
                # Camera unavailable: presence is "unknown", never "absent"
                presence, absent_seconds = presence_tracker.update(None, time.time())
                if time.time() - last_monitor_time >= MONITOR_INTERVAL_SECONDS:
                    last_monitor_time = time.time()
                    monitor_step("unavailable", None, presence, absent_seconds, None, None)
                time.sleep(0.2)
                continue

            # ---- Face -> ROI -> mean RGB (every frame) ----
            timestamp = time.time() - start_time
            landmarks = detector.detect(frame, timestamp * 1000)
            roi = get_forehead_roi(landmarks) if landmarks is not None else None
            presence, absent_seconds = presence_tracker.update(landmarks is not None, time.time())

            if roi is not None:
                # Read the colours BEFORE drawing anything on the frame
                pixels = extract_roi_pixels(frame, roi)
                buffer.add(timestamp, mean_rgb(pixels))
            else:
                # The signal must be continuous, so start again when the face is lost
                buffer.clear()
                smoother.clear()
                heart_rate = None
                quality = None

            # ---- RGB -> rPPG -> filter -> FFT -> BPM (once per second) ----
            if timestamp - last_process_time >= PROCESS_INTERVAL_SECONDS:
                last_process_time = timestamp

                if buffer.duration() >= MIN_PULSE_SECONDS:
                    times, values = buffer.get_arrays()
                    _, uniform_rgb = resample(times, values)
                    pulse = bandpass_filter(pos(uniform_rgb, SAMPLE_RATE))

                    if buffer.duration() >= MIN_BPM_SECONDS:
                        freqs, power = power_spectrum(pulse)
                        raw_bpm, peak_ratio = find_heart_rate(freqs, power)
                        quality = signal_quality(peak_ratio) if raw_bpm is not None else "Poor"
                        if quality == "Poor":
                            smoother.clear()  # never show a heart rate from a poor signal
                        else:
                            smoother.add(raw_bpm)
                        heart_rate = smoother.value()

                        # Record the value shown to the user (poor-quality results are never recorded)
                        if heart_rate is not None:
                            st.session_state.readings.append({
                                "timestamp": datetime.now().isoformat(timespec="seconds"),
                                "bpm": round(heart_rate, 1),
                                "quality": quality,
                            })
                            show_session_status(session_status)

            # ---- Hardware + camera -> alerts, recording, panel (once per second) ----
            if time.time() - last_monitor_time >= MONITOR_INTERVAL_SECONDS:
                last_monitor_time = time.time()
                monitor_step("ok", landmarks is not None, presence, absent_seconds, heart_rate, quality)

            # ---- Status shown to the user (every frame) ----
            face_status.metric("Face", "Detected" if landmarks is not None else "Not detected")
            heart_rate_metric.metric("Estimated Heart Rate", f"{heart_rate:.0f} BPM" if heart_rate else "--")
            quality_metric.metric("Signal Quality", quality or "--")

            if landmarks is None:
                signal_status.warning("Face not detected")
                signal_progress.empty()
            elif roi is None:
                signal_status.warning("Forehead too small - move closer to the camera.")
                signal_progress.empty()
            elif quality is None:
                signal_status.info(f"Collecting signal... {buffer.duration():.0f} / {MIN_BPM_SECONDS:.0f} s")
                signal_progress.progress(min(buffer.duration() / MIN_BPM_SECONDS, 1.0))
            elif heart_rate is None:
                signal_status.warning(POOR_SIGNAL_MESSAGE)
                signal_progress.empty()
            else:
                signal_status.success("Measuring - please stay still")
                signal_progress.empty()

            # Draw overlays last, so they never affect the measured colours
            if landmarks is not None and show_landmarks:
                draw_landmarks(frame, landmarks)
            if roi is not None:
                draw_roi(frame, roi)
            video_placeholder.image(frame, channels="BGR", width="stretch")
    finally:
        if camera is not None:
            camera.release()
        if detector is not None:
            detector.close()
