"""PulseVision - Streamlit dashboard.

Phase 1: webcam preview, face detection, facial landmarks and forehead ROI.
Run with:  streamlit run app.py
"""

import time

import streamlit as st

from camera import Camera
from face import FaceMeshDetector, draw_landmarks
from roi import draw_roi, extract_roi_pixels, get_forehead_roi

st.set_page_config(page_title="PulseVision", layout="wide")
st.title("PulseVision")
st.caption("Computer Vision Based Non Contact Heart Rate Detection Using Facial Video")

col_video, col_info = st.columns([2, 1])

with col_info:
    running = st.toggle("Start measurement")
    show_landmarks = st.checkbox("Show facial landmarks")
    face_status = st.empty()
    roi_status = st.empty()
    fps_status = st.empty()

with col_video:
    video_placeholder = st.empty()

if not running:
    video_placeholder.info("Turn on 'Start measurement' to open the webcam.")
    face_status.metric("Face", "-")
    st.stop()

camera = Camera()
if not camera.is_opened():
    video_placeholder.error("Could not open the webcam. Check that it is connected and not used by another app.")
    st.stop()

detector = FaceMeshDetector()
start_time = time.time()
frame_count = 0

# Streamlit stops this loop automatically when the toggle is switched off,
# and the 'finally' block releases the webcam.
try:
    while True:
        frame = camera.read()
        if frame is None:
            video_placeholder.error("Could not read a frame from the webcam.")
            break

        timestamp_ms = (time.time() - start_time) * 1000
        landmarks = detector.detect(frame, timestamp_ms)

        if landmarks is None:
            face_status.metric("Face", "Face not detected")
            roi_status.write("")
        else:
            face_status.metric("Face", "Detected")
            if show_landmarks:
                draw_landmarks(frame, landmarks)

            roi = get_forehead_roi(landmarks)
            if roi is not None:
                draw_roi(frame, roi)
                pixels = extract_roi_pixels(frame, roi)
                roi_status.write(f"Forehead ROI: {pixels.shape[1]} x {pixels.shape[0]} pixels")
            else:
                roi_status.write("Forehead ROI too small - move closer to the camera.")

        frame_count += 1
        elapsed = time.time() - start_time
        fps_status.write(f"Frame rate: {frame_count / elapsed:.1f} FPS")

        video_placeholder.image(frame, channels="BGR", width="stretch")
finally:
    camera.release()
    detector.close()
