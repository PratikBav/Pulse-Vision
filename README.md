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
Webcam -> Face detection -> Movement check -> Forehead + cheek ROI -> Mean RGB per frame -> Rolling buffer
       -> Resample -> POS rPPG pulse signal -> Band-pass filter -> FFT -> BPM -> Smoothing -> Dashboard -> Saved readings
```

| Step | What happens | Status |
|------|--------------|--------|
| 1. Capture | OpenCV reads frames from the webcam | Done |
| 2. Face detection | MediaPipe Face Landmarker finds the face and 478 landmarks | Done |
| 3. Region of interest | A forehead box (and optionally two upper-cheek boxes) is placed using the landmarks, so it follows the head | Done |
| 4. RGB signal | The average red, green and blue value of all skin boxes together is recorded every frame with a timestamp | Done |
| 5. Rolling buffer | The last 12 seconds of RGB values are kept in memory | Done |
| 6. Pulse extraction | The RGB signal is resampled to an even 30 samples/s, then the POS (Plane-Orthogonal-to-Skin) method turns it into one pulse signal | Done |
| 7. Filtering | Butterworth band-pass filter (order 3, 0.6-3.3 Hz) removes slow drift and fast noise | Done |
| 8. Heart rate | FFT finds the strongest frequency between 40 and 180 BPM, BPM = frequency x 60 | Done |
| 9. Smoothing and quality | Moving average of the last 5 estimates (sudden jumps must be confirmed), and a Good / Fair / Poor signal quality indicator | Done |
| 9b. Movement handling | Head movement restarts the signal window; the last good reading is held meanwhile | Done |
| 10. Storage | Shown readings (timestamp, BPM, quality, session ID) saved to SQLite with **Save session** | Done |
| 11. Alerts | Pop-up when the patient leaves the camera, the heart rate is high, or it changes sharply | Done |
| 11b. Notifications | A separate Notification app shows system notifications with the condition and readings, and logs them | Done |
| 12. History | BPM history graph with average, minimum and maximum BPM | Planned |

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

1. Turn on **Start measurement** in the sidebar to open the webcam.
2. Green **Forehead** and **Cheek** boxes appear on the face when it is detected
   (switch off **Use cheeks as well as forehead** to use the forehead only).
3. Stay still while the signal is collected (**Measuring... x / 10 s**).
4. The heart-rate card then shows the **Estimated Heart Rate** (the heart icon beats at that
   rate), the **Signal Quality**, and whether the patient is detected and still.
5. The **Pulse signal** chart shows the filtered pulse waveform - each peak is one heartbeat.
   The **Heart rate trend** chart shows the readings recorded in this session.
6. Click **Save session** to store the readings in the database (`data/pulsevision.db`).
   You can save while measuring or after switching measurement off; only new readings are added.
7. Open **Signal details (debug)** to see the frequency spectrum and the raw skin colour.
8. Tick **Show facial landmarks** to see all detected face points.

If nobody is in front of the camera, the card and the video show **Patient not detected**.

Each time **Start measurement** is switched on, a new session begins (its ID is the start
date and time, e.g. `20261101-153938`). A reading is recorded once per second, only while a
heart rate is shown - readings with poor signal quality are never recorded.

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

### Movement

Head movement changes the skin colour seen by the camera far more than the pulse does, so it
would make the heart rate jump. PulseVision handles this in three ways:

1. **Movement detection** - the nose position and face width are compared with their values
   0.5 s earlier. A change of more than 6% of the face width counts as movement
   (`MotionDetector` in `face.py`). Sitting still measures about 1-3%.
2. **Restart and hold** - while moving, the signal window is thrown away, the boxes turn
   orange and the card says *Movement detected*. The last good reading stays on screen marked
   **HELD** (for up to 20 s) until a clean 10 s window has been collected again. Held values are
   never saved and never trigger alerts.
3. **Jump check** - a new estimate more than 12 BPM away from the current average is only
   accepted if the next estimates confirm it (3 in a row), so one bad window cannot move the
   reading (`BPMSmoother` in `signal_processing.py`).

In a test with a simulated steady 72 BPM pulse and three head movements, the previous method
showed up to 97 BPM after a movement; the new method stayed within 1 BPM of 72.

### Alerts

| Alert | When | In PulseVision | System notification |
|-------|------|----------------|---------------------|
| Patient not present | No face in front of the camera for **10 seconds** (once per absence) | Pop-up and a red banner until the patient returns | Yes |
| High heart rate | The heart rate stays above the threshold (default **120 BPM**, set in the sidebar) for **10 seconds** | Pop-up, and the number on the card turns red | Yes |
| Heart rate fluctuation | The heart rate changes by more than **25 BPM** within **30 seconds** (at most once a minute) | Quiet pop-up | Off by default |

Every alert records the **condition** that fired and the **readings at that moment** (current or
last heart rate, signal quality, since when the condition has been true, lowest/highest values).
Only accepted readings count for the heart-rate alerts - poor-quality and held values never
trigger them. Alerts are saved in the `alerts` table of `data/pulsevision.db`. The limits are
constants at the top of `alerts.py`. These alerts are based on an *estimated* heart rate and
are **not medical alarms**.

## Notification app

`notification_app.py` is a second, small app that runs next to PulseVision on the same device.
It shows PulseVision's alerts as **system notifications** (Windows notifications, which also stay
in the Windows notification centre) and keeps a **log** of every alert.

```bash
streamlit run notification_app.py --server.port 8502
```

It opens at http://localhost:8502. Keep it running while PulseVision is measuring. The page
refreshes while it is visible; the notifications themselves come from a background watcher, so
they keep working when the page is in a background tab or closed (as long as the app is running).

How it works:

1. PulseVision saves each alert to the local SQLite database (`alerts` table).
2. A background watcher in the Notification app checks that table every second.
3. For each new alert it shows a notification, for example:

   > **PulseVision: High heart rate**
   > Condition: Heart rate above 120 BPM for 10 s
   > Current heart rate 128 BPM (Good signal). Above the limit since 14:05:10 (10 s): lowest 122,
   > highest 131 BPM. (at 14:05:20)

4. It records what it did in the same row (`notify_status`, `notified_at`), e.g. *Notification shown*.

The page shows the log (time, alert, condition, readings, heart rate, signal, notification status,
session) with a filter and a **Download log (CSV)** button. In the sidebar you choose which alerts
create a notification (alerts that are switched off are still logged) and can send a test
notification. Alerts raised while the Notification app was not running are logged but not
notified when it starts, so old alerts do not pop up all at once.

Notifications use the operating system's own notification system (on Windows through PowerShell,
shown as "Windows PowerShell"), so nothing extra is installed and nothing leaves the device.
If no notification appears, check that Windows notifications and Focus Assist / Do Not Disturb allow it.

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
├── face.py                 Face detection and landmarks (MediaPipe), head-movement detection
├── roi.py                  Forehead and cheek regions of interest, mean RGB
├── rppg.py                 POS method: RGB signal -> pulse signal
├── signal_processing.py    RGB buffer, resampling, band-pass filter, FFT, BPM, quality, smoothing
├── database.py             SQLite storage of readings and alerts
├── alerts.py               Alerts: patient not present, high heart rate, fluctuation
├── notification_app.py     Notification app: system notifications and alert log
├── notifier.py             Shows a system notification on this device
├── requirements.txt
├── README.md
└── data/
    └── pulsevision.db      Created automatically on first use (readings and alerts)
```

## Privacy

- Video frames are processed in memory and are **never saved**.
- Only numbers and short text are kept: average colour values in memory, and in the local
  SQLite database the saved heart-rate readings (timestamp, BPM, quality, session ID) and the
  alerts (time, condition, readings at that moment, notification status).
- Notifications are shown only on this device; nothing is sent over the internet.

## Progress

- [x] Phase 1 - Camera, face detection, landmarks, forehead ROI, live preview
- [x] Phase 2 - Mean RGB per frame, timestamps, rolling 12 s buffer, live RGB plots
- [x] Phase 3 - POS rPPG pulse extraction and live pulse waveform
- [x] Phase 4 - Band-pass filter, FFT, BPM, smoothing and signal quality (end-to-end heart rate)
- [x] SQLite storage of readings with a Save session button
- [x] Alerts (pop-ups): patient not present, high heart rate (adjustable threshold), sharp fluctuations
- [x] Notification app: system notifications on this device with condition and readings, plus an alert log
- [x] Movement handling (restart and hold, jump check), cheek regions and a redesigned dashboard
- [ ] Heart-rate history graph with average, minimum and maximum BPM
- [ ] Validation against a reference device (pulse oximeter / smartwatch)
