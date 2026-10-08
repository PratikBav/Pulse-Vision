"""PulseVision - Streamlit dashboard.

Phase 1: webcam preview, face detection, facial landmarks and forehead ROI.
Phase 2: mean RGB extraction from the forehead, rolling 12 s buffer, live RGB plots.
Phase 3: POS rPPG pulse waveform.
Run with:  streamlit run app.py
"""

import time

import pandas as pd
import streamlit as st

from camera import Camera
from face import FaceMeshDetector, draw_landmarks
from roi import draw_roi, extract_roi_pixels, get_forehead_roi, mean_rgb
from rppg import pos
from signal_processing import RGBBuffer

CHART_INTERVAL_SECONDS = 1.0  # redrawing charts every frame would slow the video down
MIN_PULSE_SECONDS = 3.0       # need a few seconds of signal before POS gives a useful waveform
RGB_COLORS = ["#e53935", "#43a047", "#1e88e5"]
PULSE_COLOR = "#d81b60"


def draw_line_chart(placeholder, df, columns, colors, y_title):
    """Draw a line chart of df[columns] against df["Time (s)"].

    Uses st.vega_lite_chart because st.line_chart takes ~60 ms per call,
    which pauses frame capture and leaves gaps in the pulse signal.
    """
    spec = {
        "height": 250,
        "mark": {"type": "line", "strokeWidth": 1.5},
        "transform": [{"fold": columns, "as": ["Signal", "Value"]}],
        "encoding": {
            "x": {"field": "Time (s)", "type": "quantitative"},
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

st.set_page_config(page_title="PulseVision", layout="wide")
st.title("PulseVision")
st.caption("Computer Vision Based Non Contact Heart Rate Detection Using Facial Video")

col_video, col_info = st.columns([2, 1])

with col_info:
    running = st.toggle("Start measurement")
    show_landmarks = st.checkbox("Show facial landmarks")
    face_status = st.empty()
    signal_status = st.empty()
    signal_progress = st.empty()
    rgb_status = st.empty()
    roi_status = st.empty()
    fps_status = st.empty()

with col_video:
    video_placeholder = st.empty()

st.subheader("Pulse signal (POS)")
st.caption("Pulse waveform extracted from the forehead colour - each peak should be one heartbeat")
pulse_chart = st.empty()

with st.expander("RGB signal (debug)"):
    col_raw, col_norm = st.columns(2)
    with col_raw:
        st.caption("Mean forehead colour (0-255)")
        raw_chart = st.empty()
    with col_norm:
        st.caption("Change from each channel's average (%) - the tiny pulse changes are visible here")
        norm_chart = st.empty()

if not running:
    video_placeholder.info("Turn on 'Start measurement' to open the webcam.")
    face_status.metric("Face", "-")
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

# Streamlit stops this loop automatically when the toggle is switched off,
# and the 'finally' block releases the webcam.
try:
    detector = FaceMeshDetector()
    start_time = time.time()
    last_chart_time = 0.0
    while True:
        frame = camera.read()
        if frame is None:
            video_placeholder.error("Could not read a frame from the webcam.")
            break

        timestamp = time.time() - start_time
        landmarks = detector.detect(frame, timestamp * 1000)
        roi = get_forehead_roi(landmarks) if landmarks is not None else None

        if landmarks is None:
            face_status.metric("Face", "Face not detected")
            roi_status.write("")
            rgb_status.write("")
            # The signal must be continuous, so start again when the face is lost
            buffer.clear()
        elif roi is None:
            face_status.metric("Face", "Detected")
            roi_status.write("Forehead ROI too small - move closer to the camera.")
            buffer.clear()
        else:
            face_status.metric("Face", "Detected")
            # Read the colours BEFORE drawing anything on the frame
            pixels = extract_roi_pixels(frame, roi)
            rgb = mean_rgb(pixels)
            buffer.add(timestamp, rgb)

            roi_status.write(f"Forehead ROI: {pixels.shape[1]} x {pixels.shape[0]} pixels")
            rgb_status.write(f"Mean RGB: R={rgb[0]:.1f}  G={rgb[1]:.1f}  B={rgb[2]:.1f}")

        # Signal buffer status
        if landmarks is None:
            signal_status.warning("Face not detected")
            signal_progress.empty()
        elif buffer.duration() < buffer.window_seconds * 0.95:
            signal_status.info(f"Collecting signal... {buffer.duration():.1f} / {buffer.window_seconds} s")
            signal_progress.progress(min(buffer.duration() / buffer.window_seconds, 1.0))
        else:
            signal_status.success(f"Signal buffer full ({buffer.window_seconds} s, {len(buffer)} samples)")
            signal_progress.empty()

        # Live charts, refreshed once per second
        if timestamp - last_chart_time >= CHART_INTERVAL_SECONDS:
            last_chart_time = timestamp
            times, values = buffer.get_arrays()

            if buffer.duration() < MIN_PULSE_SECONDS:
                pulse_chart.info("Collecting signal...")
            else:
                pulse = pos(values, buffer.sampling_rate())
                # Scale to a standard deviation of 1 so the chart range stays steady
                if pulse.std() > 0:
                    pulse = pulse / pulse.std()
                pulse_df = pd.DataFrame({"Time (s)": times, "Pulse": pulse})
                draw_line_chart(pulse_chart, pulse_df, ["Pulse"], [PULSE_COLOR], "Pulse")

            if len(buffer) > 1:
                raw = pd.DataFrame(values, columns=["R", "G", "B"])
                raw["Time (s)"] = times
                norm = pd.DataFrame((values / values.mean(axis=0) - 1) * 100, columns=["R", "G", "B"])
                norm["Time (s)"] = times
                draw_line_chart(raw_chart, raw, ["R", "G", "B"], RGB_COLORS, "Mean value")
                draw_line_chart(norm_chart, norm, ["R", "G", "B"], RGB_COLORS, "Change (%)")
                fps_status.write(f"Sampling rate: {buffer.sampling_rate():.1f} samples/s")

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
