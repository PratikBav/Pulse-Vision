# PulseVision

**Computer Vision Based Non Contact Heart Rate Detection Using Facial Video**

PulseVision estimates heart rate from a webcam video of the face using
remote photoplethysmography (rPPG). It is a college project, not a medical device.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

The first run downloads the official MediaPipe face model
(`models/face_landmarker.task`, about 3.6 MB).

## Files

| File | Responsibility |
|------|----------------|
| `app.py` | Streamlit dashboard |
| `camera.py` | Webcam access (OpenCV) |
| `face.py` | Face detection and 478 facial landmarks (MediaPipe Face Landmarker) |
| `roi.py` | Forehead region of interest from landmarks |

## Progress

- [x] Phase 1 — Camera, face detection, landmarks, forehead ROI, live preview
