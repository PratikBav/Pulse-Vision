"""Skin regions of interest (ROI) built from MediaPipe facial landmarks.

The forehead is the main region. The two cheeks can be added to it: more skin
pixels means the random noise averages out better, so the pulse is clearer.
"""

import cv2
import numpy as np

# MediaPipe Face Mesh landmark indices used to locate the forehead
FOREHEAD_TOP = 10           # top-centre of the forehead
FOREHEAD_LEFT = 67          # left side of the forehead
FOREHEAD_RIGHT = 297        # right side of the forehead
LEFT_EYEBROW_TOP = 107      # inner end of the left eyebrow
RIGHT_EYEBROW_TOP = 336     # inner end of the right eyebrow

# Landmarks around each upper cheek: (outer side, inner side near the nose, under the eye, lower cheek)
LEFT_CHEEK = (123, 36, 118, 205)
RIGHT_CHEEK = (352, 266, 347, 425)


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


def get_cheek_roi(landmarks, points):
    """Return one upper-cheek box as (x1, y1, x2, y2), or None if it is too small.

    The box lies between the side of the face and the nose, below the eye and
    above the moustache line. A small margin is removed on every side so the
    box stays on skin.
    """
    outer, inner, under_eye, lower = (landmarks[i] for i in points)
    x1, x2 = sorted((outer[0], inner[0]))
    y1, y2 = under_eye[1], lower[1]
    margin_x = 0.10 * (x2 - x1)
    margin_y = 0.20 * (y2 - y1)
    x1, x2 = int(x1 + margin_x), int(x2 - margin_x)
    y1, y2 = int(y1 + margin_y), int(y2 - margin_y)

    if x2 - x1 < 6 or y2 - y1 < 4:
        return None
    return x1, y1, x2, y2


def get_rois(landmarks, use_cheeks=True):
    """Return a dict {name: box} of the skin regions to use. Empty if the forehead is not usable."""
    forehead = get_forehead_roi(landmarks)
    if forehead is None:
        return {}
    rois = {"Forehead": forehead}
    if use_cheeks:
        for name, points in (("Left cheek", LEFT_CHEEK), ("Right cheek", RIGHT_CHEEK)):
            cheek = get_cheek_roi(landmarks, points)
            if cheek is not None:
                rois[name] = cheek
    return rois


def extract_roi_pixels(frame, roi):
    """Return only the pixels inside the ROI box."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    return frame[y1:y2, x1:x2]


def mean_rgb(pixels):
    """Return the average (R, G, B) of the ROI pixels.

    OpenCV stores pixels in B, G, R order, so the result is reordered to R, G, B.
    """
    b, g, r = pixels.reshape(-1, 3).mean(axis=0)
    return float(r), float(g), float(b)


def mean_rgb_of_rois(frame, rois):
    """Average (R, G, B) over all pixels of all regions together, and the pixel count."""
    pixels = [extract_roi_pixels(frame, box).reshape(-1, 3) for box in rois.values()]
    pixels = np.concatenate(pixels)
    return mean_rgb(pixels), len(pixels)


def draw_roi(frame, roi, label="Forehead", color=(0, 255, 0)):
    """Draw the ROI box with a label on the frame."""
    x1, y1, x2, y2 = roi
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    if label:
        cv2.putText(frame, label, (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
