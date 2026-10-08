"""Face detection and facial landmarks using MediaPipe Face Landmarker."""

import os
import urllib.request

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "face_landmarker.task")


def ensure_model():
    """Download the official MediaPipe face model the first time the app runs."""
    if not os.path.exists(MODEL_PATH):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    return MODEL_PATH


class FaceMeshDetector:
    """Finds one face in a frame and returns its 478 landmarks in pixel coordinates."""

    def __init__(self):
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=ensure_model()),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        self.last_timestamp_ms = -1

    def detect(self, frame_bgr, timestamp_ms):
        """Return a list of (x, y) pixel points, or None when no face is found."""
        # VIDEO mode requires strictly increasing timestamps
        timestamp_ms = max(int(timestamp_ms), self.last_timestamp_ms + 1)
        self.last_timestamp_ms = timestamp_ms

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
        result = self.landmarker.detect_for_video(mp_image, timestamp_ms)

        if not result.face_landmarks:
            return None

        # Landmarks are normalised (0..1), so convert them to pixel positions
        h, w = frame_bgr.shape[:2]
        return [(int(p.x * w), int(p.y * h)) for p in result.face_landmarks[0]]

    def close(self):
        self.landmarker.close()


def draw_landmarks(frame, landmarks, color=(0, 255, 0)):
    """Draw every landmark as a small dot (useful for checking detection)."""
    for x, y in landmarks:
        cv2.circle(frame, (x, y), 1, color, -1)
