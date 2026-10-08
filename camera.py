"""Webcam access using OpenCV."""

import sys
import threading

import cv2

# Only one Camera may be open at a time. Opening the same webcam from two
# threads at once (e.g. the dashboard open in two browser tabs) crashes
# OpenCV on Windows, so every Camera must hold this lock while it is open.
_camera_lock = threading.Lock()


class Camera:
    """Opens the webcam and returns frames one at a time."""

    def __init__(self, index=0, width=640, height=480, wait_seconds=3):
        self.cap = None
        # Wait briefly so a previous run (e.g. after a page refresh) can release the webcam
        if not _camera_lock.acquire(timeout=wait_seconds):
            return
        try:
            # CAP_DSHOW opens the webcam much faster on Windows
            backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(index, backend)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        except Exception:
            self.cap = None
            _camera_lock.release()
            raise

    def is_opened(self):
        return self.cap is not None and self.cap.isOpened()

    def read(self):
        """Return the next frame as a BGR image, or None if it could not be read."""
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok:
            return None
        # Mirror the image so it behaves like a mirror for the user
        return cv2.flip(frame, 1)

    def release(self):
        """Close the webcam and let another Camera open it."""
        if self.cap is not None:
            self.cap.release()
            self.cap = None
            _camera_lock.release()
