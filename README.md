# PulseVision

**Computer Vision Based Non Contact Heart Rate Detection Using Facial Video**

PulseVision estimates a person's heart rate using only a laptop webcam and their face.
No sensor touches the body. It uses **remote photoplethysmography (rPPG)**: every
heartbeat pushes blood into the skin, which changes the skin colour very slightly
(about 1%). The camera can pick up this tiny change even though our eyes cannot.

> **Disclaimer:** PulseVision shows an *Estimated Heart Rate*. It is a college
> project for learning purposes and is **not a medical device**. Do not use it for

## How it works

```
Webcam -> Face detection -> Forehead ROI -> Mean RGB per frame -> Rolling buffer -> Resample
       -> POS rPPG pulse signal -> Band-pass filter -> FFT -> BPM -> Smoothing -> Dashboard -> Saved readings
```

| Step | What happens | Status |
|------|--------------|--------|
| 1. Capture | OpenCV reads frames from the webcam | Done |
| 2. Face detection | MediaPipe Face Landmarker finds the face and 478 landmarks | Done |
| 3. Region of interest | A forehead box is placed using the landmarks, so it follows the head | Done |
| 4. RGB signal | The average red, green and blue value of the forehead is recorded every frame with a timestamp | Done |
| 5. Rolling buffer | The last 12 seconds of RGB values are kept in memory | Done |
| 6. Pulse extraction | The RGB signal is resampled to an even 30 samples/s, then the POS (Plane-Orthogonal-to-Skin) method turns it into one pulse signal | Done |
| 7. Filtering | Butterworth band-pass filter (order 3, 0.6-3.3 Hz) removes slow drift and fast noise | Done |
| 8. Heart rate | FFT finds the strongest frequency between 40 and 180 BPM, BPM = frequency x 60 | Done |
| 9. Smoothing and quality | Moving average of the last 5 estimates, and a Good / Fair / Poor signal quality indicator | Done |
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



Then install the packages:



## Run

```bash
streamlit run app.py
```

The dashboard opens in the browser at http://localhost:8501.

1. Turn on **Start measurement** to open the webcam.
2. A green **Forehead** box appears on your face when it is detected.
3. Stay still while the signal is collected (**Collecting signal... x / 10 s**).
4. The **Estimated Heart Rate** and **Signal Quality** are then updated every second.
5. The **Pulse signal** chart shows the filtered pulse waveform - each peak is one heartbeat.
6. Open **Signal details (debug)** to see the frequency spectrum and the raw forehead colour.
7. Tick **Show facial landmarks** to see all detected face points.

Open the dashboard in **only one browser tab** - the webcam can be used by one tab at a time.

### Signal quality

The quality is measured from the frequency spectrum of the pulse: what share of the
signal's power (between 40 and 180 BPM) lies within +/-6 BPM of the strongest peak.

| Quality | Peak power ratio | What the app does |
|---------|------------------|-------------------|
| Good | 0.60 or more | Shows the estimated heart rate |
| Fair | 0.45 - 0.60 | Shows the estimated heart rate |
| Poor | below 0.45 | Shows no heart rate and asks you to stay still and improve lighting |

A clean pulse puts most of its power in one clear peak; noise and movement spread it out.
The thresholds were chosen by testing with synthetic signals, where pure noise stayed
below 0.45 and clear pulses scored about 0.7-0.9.

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
├── signal_processing.py    RGB buffer, resampling, band-pass filter, FFT, BPM, quality, smoothing
├── requirements.txt
└── README.md
```

## Privacy

- Video frames are processed in memory and are **never saved**.
- Only numbers (average colour values, and later heart-rate readings) are kept.

## Progress

- [x] Phase 1 - Camera, face detection, landmarks, forehead ROI, live preview
- [x] Phase 2 - Mean RGB per frame, timestamps, rolling 12 s buffer, live RGB plots
- [x] Phase 3 - POS rPPG pulse extraction and live pulse waveform
- [x] Phase 4 - Band-pass filter, FFT, BPM, smoothing and signal quality (end-to-end heart rate)
- [ ] SQLite storage and heart-rate history
- [ ] Validation against a reference device (pulse oximeter / smartwatch)
