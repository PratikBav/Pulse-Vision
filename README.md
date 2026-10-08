# PulseVision

**Computer Vision Based Non Contact Heart Rate Detection Using Facial Video**

PulseVision estimates a person's heart rate using only a laptop webcam and their face.
No sensor touches the body. It uses **remote photoplethysmography (rPPG)**: every
heartbeat pushes blood into the skin, which changes the skin colour very slightly
(about 1%). The camera can pick up this tiny change even though our eyes cannot.

> **Disclaimer:** PulseVision shows an *Estimated Heart Rate*. It is a college
> project for learning purposes and is **not a medical device**. Do not use it for
> diagnosis or treatment.

## How it works

```
Webcam -> Face detection -> Forehead ROI -> Mean RGB per frame -> Rolling buffer
       -> POS rPPG pulse signal -> Band-pass filter -> FFT -> BPM -> Dashboard -> Saved readings
```

| Step | What happens | Status |
|------|--------------|--------|
| 1. Capture | OpenCV reads frames from the webcam | Done |
| 2. Face detection | MediaPipe Face Landmarker finds the face and 478 landmarks | Done |
| 3. Region of interest | A forehead box is placed using the landmarks, so it follows the head | Done |
| 4. RGB signal | The average red, green and blue value of the forehead is recorded every frame with a timestamp | Done |
| 5. Rolling buffer | The last 12 seconds of RGB values are kept in memory | Done |
| 6. Pulse extraction | POS (Plane-Orthogonal-to-Skin) method turns RGB into one pulse signal, shown as a live waveform | Done |
| 7. Filtering | Butterworth band-pass filter keeps 40-180 BPM (0.67-3 Hz) | Planned |
| 8. Heart rate | FFT finds the strongest frequency, BPM = frequency x 60 | Planned |
| 9. Smoothing and quality | Stable displayed value and a Good / Fair / Poor signal quality indicator | Planned |
| 10. Storage and history | Readings saved to SQLite with average, minimum and maximum BPM | Planned |

## Technology

Python 3.10+, OpenCV, MediaPipe, NumPy, SciPy, Streamlit, Pandas and SQLite.
Everything runs locally on a normal laptop - no cloud services.

## Setup

1. Install Python 3.10 or newer.
2. Create a virtual environment and install the requirements:

```bash
python -m venv .venv
```

Activate it on Windows:

```bash
.venv\Scripts\activate
```

or on macOS / Linux:

```bash
source .venv/bin/activate
```

Then install the packages:

```bash
pip install -r requirements.txt
```

The first run downloads the official MediaPipe face model
(`models/face_landmarker.task`, about 3.6 MB) automatically.

## Run

```bash
streamlit run app.py
```

The dashboard opens in the browser at http://localhost:8501.

1. Turn on **Start measurement** to open the webcam.
2. A green **Forehead** box appears on your face when it is detected.
3. Stay still while the signal buffer fills (**Collecting signal... x / 12 s**).
4. The **Pulse signal (POS)** chart shows the extracted pulse waveform - each peak is one heartbeat.
5. Open **RGB signal (debug)** to see how the raw forehead colour changes over time.
6. Tick **Show facial landmarks** to see all detected face points.

Open the dashboard in **only one browser tab** - the webcam can be used by one tab at a time.

## Tips for a good signal

- Sit about **50-70 cm** from the webcam and face it directly.
- Use steady light falling on the **front of your face** (avoid a bright window behind you).
- Keep your forehead visible - move hair away from it.
- **Stay still and avoid talking** while measuring. Movement causes colour changes far
  larger than the pulse itself.

## Project structure

```
PulseVision/
├── app.py                  Streamlit dashboard and main loop
├── camera.py               Webcam access (OpenCV)
├── face.py                 Face detection and landmarks (MediaPipe)
├── roi.py                  Forehead region of interest and mean RGB
├── rppg.py                 POS method: RGB signal -> pulse signal
├── signal_processing.py    Rolling RGB buffer (filtering and FFT to be added)
├── requirements.txt
└── README.md
```

## Privacy

- Video frames are processed in memory and are **never saved**.
- Only numbers (average colour values, and later heart-rate readings) are kept.
- Everything runs on your own computer; nothing is uploaded.

## Progress

- [x] Phase 1 - Camera, face detection, landmarks, forehead ROI, live preview
- [x] Phase 2 - Mean RGB per frame, timestamps, rolling 12 s buffer, live RGB plots
- [x] Phase 3 - POS rPPG pulse extraction and live pulse waveform
- [ ] Filtering, FFT and BPM estimation
- [ ] Smoothing and signal quality indicator
- [ ] SQLite storage and heart-rate history
- [ ] Validation against a reference device (pulse oximeter / smartwatch)
