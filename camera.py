"""Webcam access using OpenCV."""

import sys

import cv2


class Camera:
    """Opens the webcam and returns frames one at a time."""

    def __init__(self, index=0, width=640, height=480):
        # CAP_DSHOW opens the webcam much faster on Windows
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def is_opened(self):
        return self.cap.isOpened()

    def read(self):
        """Return the next frame as a BGR image, or None if it could not be read."""
        ok, frame = self.cap.read()
        if not ok:
            return None
        # Mirror the image so it behaves like a mirror for the user
        return cv2.flip(frame, 1)

    def release(self):
        self.cap.release()
