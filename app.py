"""PulseVision - Streamlit dashboard.

Phase 1: webcam preview, face detection, facial landmarks and forehead ROI.
Phase 2: mean RGB extraction from the forehead, rolling 12 s buffer, live RGB plots.
Phase 3: POS rPPG pulse waveform.
Phase 4: band-pass filter, FFT, BPM, smoothing and signal quality.
Storage: readings are kept per session and saved to SQLite with "Save session".
Run with:  streamlit run app.py
"""

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from camera import Camera
from database import save_readings
from face import FaceMeshDetector, draw_landmarks
from roi import draw_roi, extract_roi_pixels, get_forehead_roi, mean_rgb
from rppg import pos
from signal_processing import (MAX_BPM, MIN_BPM, SAMPLE_RATE, BPMSmoother, RGBBuffer,
                               bandpass_filter, find_heart_rate, power_spectrum, resample,
                               signal_quality)

PROCESS_INTERVAL_SECONDS = 1.0  # update heart rate and charts once per second
MIN_PULSE_SECONDS = 3.0         # need a few seconds of signal before showing the waveform
MIN_BPM_SECONDS = 10.0          # need several heartbeats before estimating the heart rate
POOR_SIGNAL_MESSAGE = "Signal quality is poor. Please remain still and improve lighting."
RGB_COLORS = ["#e53935", "#43a047", "#1e88e5"]
PULSE_COLOR = "#d81b60"


def draw_line_chart(placeholder, df, x, columns, colors, y_title):
    """Draw a line chart of df[columns] against df[x].

    Uses st.vega_lite_chart because st.line_chart takes ~60 ms per call,
    which pauses frame capture and leaves gaps in the pulse signal.
    """
    spec = {
        "height": 250,
        "mark": {"type": "line", "strokeWidth": 1.5},
        "transform": [{"fold": columns, "as": ["Signal", "Value"]}],
        "encoding": {
            "x": {"field": x, "type": "quantitative"},
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


def show_session_status(placeholder):
    """Show how many readings the current session has, and how many are not saved yet."""
    session_id = st.session_state.get("session_id")
    if session_id is None:
        placeholder.caption("No session yet - start a measurement.")
        return
    total = len(st.session_state.readings)
    unsaved = total - st.session_state.saved_count
    placeholder.caption(f"Session {session_id}: {total} readings, {unsaved} not saved")


st.set_page_config(page_title="PulseVision", layout="wide")
st.title("PulseVision")
st.caption("Computer Vision Based Non Contact Heart Rate Detection Using Facial Video")

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
    debug_status = st.empty()

with col_video:
    video_placeholder = st.empty()

st.subheader("Pulse signal")
st.caption("POS pulse after the band-pass filter - each peak should be one heartbeat")
pulse_chart = st.empty()

with st.expander("Signal details (debug)"):
    st.caption("Frequency spectrum of the pulse - the highest peak is the heart rate")
    spectrum_chart = st.empty()
    col_raw, col_norm = st.columns(2)
    with col_raw:
        st.caption("Mean forehead colour (0-255)")
        raw_chart = st.empty()
    with col_norm:
        st.caption("Change from each channel's average (%)")
        norm_chart = st.empty()

if not running:
    video_placeholder.info("Turn on 'Start measurement' to open the webcam.")
    heart_rate_metric.metric("Estimated Heart Rate", "--")
    st.stop()

camera = Camera()
if not camera.is_opened():
    camera.release()
    video_placeholder.error(
        "Could not open the webcam. Check that it is connected and not already "
        "in use by another app or another PulseVision browser tab."
    )
    st.stop()

detector = None
buffer = RGBBuffer()
smoother = BPMSmoother()

# Streamlit stops this loop automatically when the toggle is switched off,
# and the 'finally' block releases the webcam.
try:
    detector = FaceMeshDetector()
    start_time = time.time()
    last_process_time = 0.0
    heart_rate = None  # smoothed BPM shown to the user, None when not available
    quality = None     # "Good" / "Fair" / "Poor", None while collecting signal
    while True:
        frame = camera.read()
        if frame is None:
            video_placeholder.error("Could not read a frame from the webcam.")
            break

        # ---- Face -> ROI -> mean RGB (every frame) ----
        timestamp = time.time() - start_time
        landmarks = detector.detect(frame, timestamp * 1000)
        roi = get_forehead_roi(landmarks) if landmarks is not None else None

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

            if buffer.duration() < MIN_PULSE_SECONDS:
                pulse_chart.info("Collecting signal...")
            else:
                times, values = buffer.get_arrays()
                uniform_times, uniform_rgb = resample(times, values)
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

                    in_range = (freqs * 60 >= MIN_BPM - 10) & (freqs * 60 <= MAX_BPM + 10)
                    spectrum = pd.DataFrame({"Heart rate (BPM)": freqs[in_range] * 60,
                                             "Power": power[in_range] / power[in_range].max()})
                    draw_line_chart(spectrum_chart, spectrum, "Heart rate (BPM)", ["Power"],
                                    [PULSE_COLOR], "Relative power")
                    raw_text = f"{raw_bpm:.0f} BPM" if raw_bpm is not None else "invalid"
                    debug_status.caption(
                        f"Latest estimate: {raw_text} | peak power ratio: {peak_ratio:.2f} | "
                        f"sampling rate: {buffer.sampling_rate():.1f} samples/s"
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
    camera.release()
    if detector is not None:
        detector.close()
