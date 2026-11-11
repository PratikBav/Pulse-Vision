# PulseVision

**Computer Vision & Hardware Sensor Non-Contact / Contact Patient Monitoring Prototype**

PulseVision combines:
1. **Computer-vision heart rate estimation (rPPG)** using a laptop webcam and MediaPipe Face Landmarker.
2. **Hardware sensor measurements** from an Arduino UNO R4 WiFi over USB serial:
   - **Sensor 1:** `MAX30100` pulse oximeter (heart rate BPM & uncalibrated SpO2 %).
   - **Sensor 2:** `MPU9250 / MPU6500 / MPU9255` 6/9-axis IMU (3-axis acceleration, angular velocity, and peak dynamic motion `motion_g` in `g`).
3. **Unified monitoring, configurable alert rules, local SQLite logging, and SMTP email notifications.**

> **Disclaimer:** PulseVision shows an *Estimated Heart Rate* and *uncalibrated SpO2*. It is a college
> project for learning purposes and is **not a medical device**. Do not use it for clinical decisions.

## How it works

```
Webcam  -> Face detection -> Forehead ROI -> Mean RGB -> Rolling buffer -> Resample -> POS rPPG -> Band-pass -> FFT -> BPM
Arduino -> MAX30100 + MPU9250/6500/9255 -> USB Serial (NDJSON @ 115200) -> Background SerialReader
        -> Unified Snapshot (1 Hz) -> AlertEngine (HR / SpO2 / Motion / Absence / Disagreement / Faults)
        -> SQLite (monitor_log & alerts) + Non-blocking SMTP Email Notifier
```

## Setup

1. Install Python 3.10 or newer.
2. Create a virtual environment and install the requirements:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

The dashboard opens in the browser at http://localhost:8501.

## Arduino Hardware Setup

1. **Firmware:** Open `firmware/pulsevision_sensors/pulsevision_sensors.ino` in the Arduino IDE (or compile with `arduino-cli` for `arduino:renesas_uno:unor4wifi`) and upload it to your **Arduino UNO R4 WiFi**.
   - **Wiring (shared I2C bus on pins `A4` SDA / `A5` SCL, 3.3V):**
     - `MAX30100` (I2C `0x57`): `VIN -> 3.3V`, `GND -> GND`, `SDA -> A4`, `SCL -> A5`
     - `MPU9250 / MPU6500 / MPU9255` (I2C `0x68` or `0x69` auto-detected): `VCC -> 3.3V`, `GND -> GND`, `SDA -> A4`, `SCL -> A5`
   - Note: Close the Arduino IDE Serial Monitor before connecting in PulseVision so the COM port is not locked.
2. **Serial format (115200 baud, newline-delimited JSON, 1 Hz):**
   ```json
   {"t_ms":12345,"max30100":{"ok":true,"hr_bpm":72.4,"spo2_pct":97},"mpu":{"ok":true,"ax":-0.02,"ay":0.01,"az":0.99,"a_g":1.00,"motion_g":0.02,"gx":0.4,"gy":-0.2,"gz":0.1}}
   ```
   - `motion_g` is the peak dynamic acceleration (`|accelSqrt() - 1.0g|`, sampled at 20 Hz over each 1 s window) so sudden patient movement, agitation, or falls between 1 s reports are captured (`~0.00 g` at rest).
   - Select **`MOCK (simulated)`** in the sidebar port dropdown to test the full pipeline without physical hardware (simulated readings are explicitly labeled).

## Configuring Alerts & Email Notifications

- All alert rules start **disabled** (`None`) — no clinical thresholds are hardcoded.
- Open the sidebar **Alert settings** expander to configure:
  - Upper/lower limits for **Sensor HR**, **Sensor SpO2**, **Sensor motion / agitation (`g`)**, and **Camera HR**.
  - Patient absence duration (seconds without face detection).
  - Sensor vs. camera HR disagreement tolerance (BPM).
  - Arduino disconnect / sensor fault (`MAX30100` or `MPU`) alerts, confirmation time, and cooldown.
  - Recipient email address.
- For email delivery, copy `.env.example` to `.env` and set `PV_SMTP_HOST`, `PV_SMTP_PORT`, `PV_SMTP_USER`, `PV_SMTP_PASSWORD`, and `PV_SMTP_FROM`.

## Running Tests

```bash
pytest -q
```

## Firebase Notifications

The dashboard includes a refreshable **Notifications & Alerts** tab with a
server-side Firebase/Firestore integration and optional FCM/MSG91 delivery.
Setup, required credentials, delivery-state semantics, and deployment commands
are documented in [FIREBASE_NOTIFICATIONS.md](./FIREBASE_NOTIFICATIONS.md).
