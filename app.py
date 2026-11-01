"""PulseVision - Streamlit dashboard.

Phase 1: webcam preview, face detection, facial landmarks and forehead ROI.
Phase 2: mean RGB extraction, rolling 12 s buffer, live RGB plots.
Phase 3: POS rPPG pulse waveform.
Phase 4: band-pass filter, FFT, BPM, smoothing and signal quality.
Storage: readings are kept per session and saved to SQLite with "Save session".
Alerts: pop-ups when the patient is not present, the heart rate is high, or it changes sharply.
Movement: when the patient moves, the signal window restarts and the last reading is held.
Run with:  streamlit run app.py
"""

import time
from datetime import datetime

import cv2
import pandas as pd
import streamlit as st

from alerts import HIGH_BPM_DEFAULT, AlertMonitor
from camera import Camera
from database import save_alert, save_readings
from face import FaceMeshDetector, MotionDetector, draw_landmarks
from roi import draw_roi, get_rois, mean_rgb_of_rois
from rppg import pos
from signal_processing import (MAX_BPM, MIN_BPM, SAMPLE_RATE, HeartRateTracker, RGBBuffer,
                               bandpass_filter, find_heart_rate, power_spectrum, resample)

PROCESS_INTERVAL_SECONDS = 1.0  # update heart rate and charts once per second
MIN_PULSE_SECONDS = 3.0         # need a few seconds of signal before showing the waveform
MIN_BPM_SECONDS = 10.0          # need several heartbeats before estimating the heart rate
MOTION_MESSAGE_SECONDS = 1.0    # keep the movement message a moment after movement stops
POOR_SIGNAL_MESSAGE = "Signal quality is poor. Please remain still and improve lighting."
RGB_COLORS = ["#e53935", "#43a047", "#1e88e5"]
PULSE_COLOR = "#e5484d"
GREEN, ORANGE = (80, 200, 120), (0, 165, 255)  # OpenCV colours are B, G, R

CSS = """
<style>
.pv-title {font-size: 2.1rem; font-weight: 800; margin: 0;}
.pv-title span {color: #e5484d;}
.pv-sub {opacity: .7; margin: .1rem 0 1rem 0;}
.pv-card {border: 1px solid rgba(128,128,128,.25); border-radius: 16px; padding: 1.1rem 1.3rem;
          background: rgba(128,128,128,.06);}
.pv-label {font-size: .75rem; text-transform: uppercase; letter-spacing: .08em; opacity: .65;}
.pv-hr {display: flex; align-items: center; gap: .8rem; margin: .3rem 0 .2rem 0;}
.pv-bpm {font-size: 3.8rem; font-weight: 800; line-height: 1;}
.pv-unit {font-size: 1.1rem; opacity: .65; margin-left: .25rem; font-weight: 600;}
.pv-heart {width: 46px; height: 46px; fill: #e5484d;}
.pv-heart.beat {animation: pv-beat 1s ease-in-out infinite;}
.pv-heart.idle {fill: rgba(128,128,128,.45);}
@keyframes pv-beat {0%, 100% {transform: scale(1);} 15% {transform: scale(1.18);} 30% {transform: scale(1);}}
.pv-tag {font-size: .72rem; font-weight: 700; border-radius: 999px; padding: .15rem .55rem;
         background: rgba(210,153,34,.2); color: #d29922; vertical-align: middle;}
.pv-pills {display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .9rem;}
.pv-pill {border-radius: 999px; padding: .28rem .75rem; font-size: .82rem; font-weight: 600;}
.pv-ok {background: rgba(46,160,67,.18); color: #3fb950;}
.pv-warn {background: rgba(210,153,34,.2); color: #d29922;}
.pv-bad {background: rgba(248,81,73,.18); color: #f85149;}
.pv-info {background: rgba(56,139,253,.18); color: #58a6ff;}
.pv-neutral {background: rgba(128,128,128,.18);}
.pv-status {margin-top: .9rem; padding: .65rem .85rem; border-radius: 10px; font-size: .95rem; font-weight: 500;}
.pv-progress {height: 6px; border-radius: 3px; background: rgba(128,128,128,.25); overflow: hidden; margin-top: .55rem;}
.pv-progress > div {height: 100%; background: #e5484d;}
.pv-note {font-size: .75rem; opacity: .6; margin-top: .8rem;}
</style>
"""

HEART_SVG = ('<svg class="pv-heart {cls}" style="animation-duration:{period:.2f}s" viewBox="0 0 24 24">'
             '<path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 2.8 4.5 6.6 4.5c2.1 0 3.6 1.1 4.4 2.6.8-1.5 2.3-2.6 '
             '4.4-2.6 3.8 0 5.7 3.9 4.2 7.3C19.5 16.4 12 21 12 21z"/></svg>')


def draw_line_chart(placeholder, df, x, columns, colors, y_title, x_type="quantitative", points=False):
    """Draw a line chart of df[columns] against df[x].

    Uses st.vega_lite_chart because st.line_chart takes ~60 ms per call,
    which pauses frame capture and leaves gaps in the pulse signal.
    """
    spec = {
        "height": 230,
        "mark": {"type": "line", "strokeWidth": 2, "point": points, "interpolate": "monotone"},
        "transform": [{"fold": columns, "as": ["Signal", "Value"]}],
        "encoding": {
            "x": {"field": x, "type": x_type, "title": None if x_type == "temporal" else x},
            "y": {"field": "Value", "type": "quantitative", "title": y_title, "scale": {"zero": False}},
            "color": {
                "field": "Signal",
                "type": "nominal",
                "scale": {"domain": columns, "range": colors},
                "legend": {"orient": "bottom"} if len(columns) > 1 else None,
            },
        },
    }
    placeholder.vega_lite_chart(df, spec, width="stretch")


def vitals_html(bpm, held, quality, patient_present, moving, status, status_kind, progress, high_bpm=None):
    """Build the HTML for the vitals card."""
    high = bpm is not None and high_bpm is not None and bpm > high_bpm
    if bpm is not None:
        heart = HEART_SVG.format(cls="beat", period=60.0 / bpm)
        value = (f'<span class="pv-bpm" style="color:{"#f85149" if high else "inherit"}">{bpm:.0f}</span>'
                 f'<span class="pv-unit">BPM</span>')
        if held:
            value += ' <span class="pv-tag">HELD</span>'
    else:
        heart = HEART_SVG.format(cls="idle", period=1)
        value = '<span class="pv-bpm">--</span><span class="pv-unit">BPM</span>'

    quality_class = {"Good": "pv-ok", "Fair": "pv-warn", "Poor": "pv-bad"}.get(quality, "pv-neutral")
    pills = [
        f'<span class="pv-pill {"pv-ok" if patient_present else "pv-bad"}">'
        f'Patient: {"Detected" if patient_present else "Not detected"}</span>',
        f'<span class="pv-pill {quality_class}">Signal quality: {quality or "Measuring"}</span>',
        f'<span class="pv-pill {"pv-warn" if moving else "pv-ok"}">'
        f'{"Movement detected" if moving else "Still"}</span>',
    ]
    if high:
        pills.append(f'<span class="pv-pill pv-bad">Above {high_bpm:.0f} BPM</span>')
    bar = (f'<div class="pv-progress"><div style="width:{progress * 100:.0f}%"></div></div>'
           if progress is not None else "")
    return (
        f'<div class="pv-card"><div class="pv-label">Estimated Heart Rate</div>'
        f'<div class="pv-hr">{heart}<div>{value}</div></div>'
        f'<div class="pv-pills">{"".join(pills)}</div>'
        f'<div class="pv-status pv-{status_kind}">{status}</div>{bar}'
        f'<div class="pv-note">Estimate from facial video - not a medical device.</div></div>'
    )


def draw_no_patient(frame):
    """Darken the video and write 'Patient not detected' across it."""
    frame[:] = (frame * 0.45).astype(frame.dtype)
    text = "Patient not detected"
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
    x, y = (frame.shape[1] - w) // 2, (frame.shape[0] + h) // 2
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (90, 90, 255), 2, cv2.LINE_AA)


def show_session_status(placeholder):
    """Show how many readings the current session has, and how many are not saved yet."""
    session_id = st.session_state.get("session_id")
    if session_id is None:
        placeholder.caption("No session yet - start a measurement.")
        return
    total = len(st.session_state.readings)
    unsaved = total - st.session_state.saved_count
    placeholder.caption(f"Session **{session_id}**: {total} readings, {unsaved} not saved")


def show_trend(placeholder):
    """Line chart of the heart rates recorded in this session."""
    readings = st.session_state.get("readings", [])
    if len(readings) < 2:
        placeholder.caption("The heart-rate trend appears here once readings are recorded.")
        return
    df = pd.DataFrame(readings).rename(columns={"timestamp": "Time", "bpm": "BPM"})
    draw_line_chart(placeholder, df, "Time", ["BPM"], [PULSE_COLOR], "BPM", x_type="temporal")


def show_alert_log(placeholder):
    """List the most recent alerts of this session."""
    alerts = st.session_state.get("alerts", [])
    if alerts:
        placeholder.markdown("\n".join(f"- {a}" for a in reversed(alerts[-5:])))
    else:
        placeholder.caption("No alerts.")


def deliver_alert(alert, log_placeholder):
    """Show the alert as a pop-up and save it (the Notification app picks it up from the database)."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    st.session_state.setdefault("alerts", []).append(f"{timestamp[11:]} **{alert.kind}** - {alert.message}")
    save_alert(st.session_state.session_id, timestamp, alert)
    show_alert_log(log_placeholder)

    icon = ":material/notifications:" if alert.quiet else ":material/warning:"
    st.toast(alert.message, icon=icon)


# ---------------------------------------------------------------- Page layout
st.set_page_config(page_title="PulseVision", page_icon=":material/favorite:", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.markdown('<div class="pv-title">Pulse<span>Vision</span></div>'
            '<div class="pv-sub">Non-contact heart rate estimation from facial video (rPPG)</div>',
            unsafe_allow_html=True)

with st.sidebar:
    st.header("Controls")
    running = st.toggle("Start measurement")
    use_cheeks = st.toggle("Use cheeks as well as forehead", value=True,
                           help="More skin pixels give a cleaner pulse signal.")
    show_landmarks = st.checkbox("Show facial landmarks")

    # st.session_state keeps values between Streamlit reruns (e.g. when a button is
    # clicked), so the readings are not lost. Switching measurement on starts a new session.
    if running and not st.session_state.get("was_running", False):
        st.session_state.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        st.session_state.readings = []
        st.session_state.saved_count = 0
        st.session_state.alerts = []
    st.session_state.was_running = running

    st.divider()
    st.subheader("Session")
    save_clicked = st.button("Save session", icon=":material/save:", width="stretch")
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

    st.divider()
    st.subheader("Alerts")
    high_bpm = st.number_input("High heart rate alert above (BPM)", min_value=80, max_value=180,
                               value=HIGH_BPM_DEFAULT, step=5, key="high_bpm",
                               help="An alert is raised as soon as a shown heart rate goes above this.")
    alert_log = st.empty()
    show_alert_log(alert_log)

col_video, col_vitals = st.columns([3, 2], gap="large")
with col_video:
    video_placeholder = st.empty()
with col_vitals:
    alert_banner = st.empty()
    vitals_card = st.empty()
    debug_status = st.empty()

col_pulse, col_trend = st.columns(2, gap="large")
with col_pulse:
    st.subheader("Pulse signal")
    st.caption("Filtered pulse waveform - each peak is one heartbeat")
    pulse_chart = st.empty()
with col_trend:
    st.subheader("Heart rate trend")
    st.caption("Readings recorded in this session")
    trend_chart = st.empty()
show_trend(trend_chart)

with st.expander("Signal details (debug)"):
    st.caption("Frequency spectrum of the pulse - the highest peak is the heart rate")
    spectrum_chart = st.empty()
    col_raw, col_norm = st.columns(2)
    with col_raw:
        st.caption("Mean skin colour (0-255)")
        raw_chart = st.empty()
    with col_norm:
        st.caption("Change from each channel's average (%)")
        norm_chart = st.empty()

if not running:
    video_placeholder.info("Turn on **Start measurement** in the sidebar to open the webcam.",
                           icon=":material/videocam:")
    vitals_card.markdown(vitals_html(None, False, None, False, False, "Not measuring", "neutral", None),
                         unsafe_allow_html=True)
    pulse_chart.caption("The pulse waveform appears here while measuring.")
    st.stop()

camera = Camera()
if not camera.is_opened():
    camera.release()
    video_placeholder.error(
        "Could not open the webcam. Check that it is connected and not already "
        "in use by another app or another PulseVision browser tab."
    )
    st.stop()

# ---------------------------------------------------------------- Measurement loop
detector = None
buffer = RGBBuffer()
tracker = HeartRateTracker()
motion = MotionDetector()
monitor = AlertMonitor()

# Streamlit stops this loop automatically when the toggle is switched off,
# and the 'finally' block releases the webcam.
try:
    detector = FaceMeshDetector()
    start_time = time.time()
    last_process_time = 0.0
    last_motion_time = -100.0
    last_card = None
    banner_shown = False
    while True:
        frame = camera.read()
        if frame is None:
            video_placeholder.error("Could not read a frame from the webcam.")
            break

        # ---- Face -> movement check -> skin regions -> mean RGB (every frame) ----
        timestamp = time.time() - start_time
        landmarks = detector.detect(frame, timestamp * 1000)
        patient_present = landmarks is not None
        rois = get_rois(landmarks, use_cheeks) if patient_present else {}

        alert = monitor.update_face(timestamp, patient_present)
        if alert:
            deliver_alert(alert, alert_log)

        moving = False
        if not patient_present:
            # Patient gone: forget the signal and the reading
            buffer.clear()
            tracker.reset()
            motion.reset()
        else:
            moving = motion.is_moving(motion.update(timestamp, landmarks))
            if moving:
                # Movement corrupts the colour signal, so throw the window away and
                # hold the last good reading until a clean window has been collected
                buffer.clear()
                tracker.restart_window()
                last_motion_time = timestamp
            elif rois:
                # Read the colours BEFORE drawing anything on the frame
                rgb, _ = mean_rgb_of_rois(frame, rois)
                buffer.add(timestamp, rgb)
            else:
                # Face too small for the skin regions: no usable signal, hold the reading
                buffer.clear()
                tracker.restart_window()

        # ---- RGB -> rPPG -> filter -> FFT -> BPM (once per second) ----
        if timestamp - last_process_time >= PROCESS_INTERVAL_SECONDS:
            last_process_time = timestamp

            if buffer.duration() >= MIN_PULSE_SECONDS:
                times, values = buffer.get_arrays()
                uniform_times, uniform_rgb = resample(times, values)
                pulse = bandpass_filter(pos(uniform_rgb, SAMPLE_RATE))

                if buffer.duration() >= MIN_BPM_SECONDS:
                    freqs, power = power_spectrum(pulse)
                    raw_bpm, peak_ratio = find_heart_rate(freqs, power)
                    accepted = tracker.update(timestamp, raw_bpm, peak_ratio)

                    # Record each accepted reading (held or poor-quality values are never recorded)
                    shown_bpm, _ = tracker.shown_value(timestamp)
                    if accepted and shown_bpm is not None:
                        st.session_state.readings.append({
                            "timestamp": datetime.now().isoformat(timespec="seconds"),
                            "bpm": round(shown_bpm, 1),
                            "quality": tracker.quality,
                        })
                        show_session_status(session_status)
                        show_trend(trend_chart)

                        # ---- Alerts: high heart rate, sharp fluctuation (quiet) ----
                        for alert in monitor.update_heart_rate(timestamp, shown_bpm, high_bpm,
                                                               tracker.quality):
                            deliver_alert(alert, alert_log)

                    in_range = (freqs * 60 >= MIN_BPM - 10) & (freqs * 60 <= MAX_BPM + 10)
                    spectrum = pd.DataFrame({"Heart rate (BPM)": freqs[in_range] * 60,
                                             "Power": power[in_range] / max(power[in_range].max(), 1e-12)})
                    draw_line_chart(spectrum_chart, spectrum, "Heart rate (BPM)", ["Power"],
                                    [PULSE_COLOR], "Relative power")
                    raw_text = f"{raw_bpm:.0f} BPM" if raw_bpm is not None else "invalid"
                    debug_status.caption(
                        f"Latest estimate: {raw_text} ({'used' if accepted else 'not used'}) | "
                        f"peak power ratio: {peak_ratio:.2f} | sampling rate: {buffer.sampling_rate():.1f}/s"
                    )

                # Charts: scale the pulse to a standard deviation of 1 so the range stays steady
                pulse_df = pd.DataFrame({"Time (s)": uniform_times, "Pulse": pulse / max(pulse.std(), 1e-12)})
                draw_line_chart(pulse_chart, pulse_df, "Time (s)", ["Pulse"], [PULSE_COLOR], "Pulse")
                raw = pd.DataFrame(values, columns=["R", "G", "B"])
                raw["Time (s)"] = times
                norm = pd.DataFrame((values / values.mean(axis=0) - 1) * 100, columns=["R", "G", "B"])
                norm["Time (s)"] = times
                draw_line_chart(raw_chart, raw, "Time (s)", ["R", "G", "B"], RGB_COLORS, "Mean value")
                draw_line_chart(norm_chart, norm, "Time (s)", ["R", "G", "B"], RGB_COLORS, "Change (%)")
            else:
                pulse_chart.caption("Collecting signal...")

        # ---- What the user sees (every frame, only sent when something changed) ----
        bpm, held = tracker.shown_value(timestamp)
        recently_moved = timestamp - last_motion_time < MOTION_MESSAGE_SECONDS
        progress = None
        if not patient_present:
            status, kind = "Patient not detected", "bad"
        elif recently_moved:
            status, kind = "Movement detected - please stay still.", "warn"
            if bpm is not None:
                status += " Holding the last reading."
        elif not rois:
            status, kind = "Move closer to the camera.", "warn"
        elif tracker.quality is None:
            status = f"Measuring... {buffer.duration():.0f} / {MIN_BPM_SECONDS:.0f} s"
            if bpm is not None:
                status += " - showing the last reading"
            kind = "info"
            progress = min(buffer.duration() / MIN_BPM_SECONDS, 1.0)
        elif tracker.quality == "Poor":
            status, kind = POOR_SIGNAL_MESSAGE, "warn"
        else:
            status, kind = "Measuring - please stay still", "ok"

        card = vitals_html(bpm, held, tracker.quality, patient_present, recently_moved, status, kind,
                           progress, high_bpm)
        if card != last_card:
            vitals_card.markdown(card, unsafe_allow_html=True)
            last_card = card

        if monitor.is_absent() != banner_shown:
            banner_shown = monitor.is_absent()
            if banner_shown:
                alert_banner.error("Patient not present in front of the camera.", icon=":material/person_off:")
            else:
                alert_banner.empty()

        # Draw overlays last, so they never affect the measured colours
        if not patient_present:
            draw_no_patient(frame)
        else:
            if show_landmarks:
                draw_landmarks(frame, landmarks)
            color = ORANGE if recently_moved else GREEN
            for name, box in rois.items():
                draw_roi(frame, box, "Forehead" if name == "Forehead" else "Cheek", color)
        video_placeholder.image(frame, channels="BGR", width="stretch")
finally:
    camera.release()
    if detector is not None:
        detector.close()
