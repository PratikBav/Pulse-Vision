"""Face detection, facial landmarks (MediaPipe Face Landmarker) and head-movement detection."""

import math
import os
import urllib.request
from collections import deque

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


class MotionDetector:
    """Detects head movement from the facial landmarks.

    It compares the nose position and the face width now with their values
    0.5 s ago. Movement is measured as a fraction of the face width, so it
    works the same whether the person sits near or far from the camera.
    Turning or tilting the head moves the nose; leaning in or out changes the width.
    """

    NOSE_TIP = 1
    FACE_LEFT = 234
    FACE_RIGHT = 454
    LOOKBACK_SECONDS = 0.5
    THRESHOLD = 0.06  # moved more than 6% of the face width in 0.5 s -> "moving"

    def __init__(self):
        self.history = deque()  # (time, nose x, nose y, face width)

    def update(self, timestamp, landmarks):
        """Add the current landmarks and return the amount of movement (0 = perfectly still)."""
        nose_x, nose_y = landmarks[self.NOSE_TIP]
        width = abs(landmarks[self.FACE_RIGHT][0] - landmarks[self.FACE_LEFT][0])
        self.history.append((timestamp, nose_x, nose_y, width))
        while timestamp - self.history[0][0] > self.LOOKBACK_SECONDS:
            self.history.popleft()
        if width == 0:
            return 0.0

        _, old_x, old_y, old_width = self.history[0]
        shift = math.hypot(nose_x - old_x, nose_y - old_y) / width
        resize = abs(width - old_width) / width
        return max(shift, resize)

    def is_moving(self, motion):
        return motion > self.THRESHOLD

    def reset(self):
        self.history.clear()


def draw_landmarks(frame, landmarks, color=(0, 255, 0)):
    """Draw every landmark as a small dot (useful for checking detection)."""
    for x, y in landmarks:
        cv2.circle(frame, (x, y), 1, color, -1)
