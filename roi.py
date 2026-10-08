"""Forehead region of interest (ROI) built from MediaPipe facial landmarks."""

import cv2

# MediaPipe Face Mesh landmark indices used to locate the forehead
FOREHEAD_TOP = 10           # top-centre of the forehead
FOREHEAD_LEFT = 109         # left side of the forehead
FOREHEAD_RIGHT = 338        # right side of the forehead
LEFT_EYEBROW_TOP = 107      # inner end of the left eyebrow
RIGHT_EYEBROW_TOP = 336     # inner end of the right eyebrow


def get_forehead_roi(landmarks):
    """Return the forehead box as (x1, y1, x2, y2) in pixels, or None if it is too small.

    The box sits between the eyebrows and the top of the forehead. We skip
    the top 20% (hairline) and the bottom 15% (eyebrows) so that the box
    contains only skin.
    """
    top_y = landmarks[FOREHEAD_TOP][1]
    brow_y = min(landmarks[LEFT_EYEBROW_TOP][1], landmarks[RIGHT_EYEBROW_TOP][1])
    height = brow_y - top_y

    # min/max so the box is correct whether or not the image is mirrored
    x1 = min(landmarks[FOREHEAD_LEFT][0], landmarks[FOREHEAD_RIGHT][0])
    x2 = max(landmarks[FOREHEAD_LEFT][0], landmarks[FOREHEAD_RIGHT][0])
    y1 = int(top_y + 0.20 * height)
    y2 = int(brow_y - 0.15 * height)

    if x2 - x1 < 10 or y2 - y1 < 5:
        return None
    return x1, y1, x2, y2


def extract_roi_pixels(frame, roi):
    """Return only the pixels inside the ROI box."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    return frame[y1:y2, x1:x2]


def draw_roi(frame, roi, label="Forehead", color=(0, 255, 0)):
    """Draw the ROI box with a label on the frame."""
    x1, y1, x2, y2 = roi
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    cv2.putText(frame, label, (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
