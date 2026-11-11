# PulseVision — Arduino Hardware & Computer-Vision Integration Report

> **Disclaimer:** PulseVision shows an *Estimated Heart Rate* and *uncalibrated SpO2*. It is an educational prototype and is **not a medical device**. Do not use it for clinical diagnosis or decision-making.

---

## 1. Summary of the Existing Codebase & Changes Made

### Existing Codebase (`Pulse-Vision/`)
- **Technology Stack:** Python 3.10+, Streamlit (`app.py`), OpenCV (`camera.py`), MediaPipe Face Landmarker (`face.py`), NumPy/SciPy (`rppg.py`, `signal_processing.py`), Pandas, and SQLite (`database.py`).
- **Original Functionality:**
  - Captures webcam frames (640x480) via OpenCV DirectShow with a process-wide camera lock.
  - Detects 478 3D facial landmarks via MediaPipe and extracts the forehead Region of Interest (ROI).
  - Computes mean RGB per frame into a 12-second rolling buffer, resamples to 30 Hz, extracts the pulse signal using the POS (Plane-Orthogonal-to-Skin) rPPG algorithm, applies a 3rd-order Butterworth band-pass filter (0.6–3.3 Hz), and estimates heart rate (40–180 BPM) via FFT with a 5-sample moving average and peak-power signal quality rating (`Good` / `Fair` / `Poor`).
  - Saves session readings (`session_id`, `timestamp`, `bpm`, `quality`) to `data/pulsevision.db` when the user clicks **Save session**.

### Integration Changes Made
1. **Dual-Sensor Arduino Firmware (`firmware/pulsevision_sensors/pulsevision_sensors.ino`):**
   - Polls **Sensor 1 (`MAX30100` pulse oximeter, I2C `0x57`)** using `MAX30100_PulseOximeter.h` and **Sensor 2 (`MPU9250 / MPU6500 / MPU9255` IMU, I2C `0x68` or `0x69` auto-detected)** using `MPU9250_asukiaaa.h`.
   - Samples the MPU accelerometer at 20 Hz to track the 1-second peak dynamic motion `motion_g = |accelSqrt() - 1.0g|` (so sudden movement, agitation, or falls between 1 Hz serial reports are never missed), alongside 3-axis acceleration (`ax`, `ay`, `az`, `a_g`) and gyroscope angular velocity (`gx`, `gy`, `gz`).
   - Emits 1 Hz newline-delimited JSON (`NDJSON`) at `115200` baud and automatically retries either sensor every 5 seconds if disconnected.
2. **Non-Blocking USB Serial Communication (`hardware.py`):**
   - Implemented `SerialReader` running in a background daemon thread (held as a `@st.cache_resource` singleton so Streamlit reruns never reopen or lock the COM port).
   - Auto-detects Arduino ports (USB VID `0x2341`), prevents duplicate port opens, automatically reconnects on disconnect, validates sensor plausibility ranges (`HR 20–250 BPM`, `SpO2 50–100 %`, `Accel ±16 g`, `Gyro ±2000 °/s`), and logs errors without exposing patient data.
   - Includes `MockSerial` (`MOCK (simulated)` port) generating clearly labeled simulated `MAX30100` + `MPU9250/6500/9255` readings.
3. **Unified Monitoring State, Presence Tracking & Storage (`monitor.py`, `database.py`):**
   - Combines hardware readings (`hw_hr_bpm`, `hw_spo2_pct`, `hw_accel_g`, `hw_motion_g`, `hw_gyro_dps`) and camera estimates (`cam_bpm`, `cam_quality`, `presence`) once per second into a `Snapshot`.
   - Tracks patient presence via `PresenceTracker`, accumulating absence duration across frames while distinguishing camera unavailability (`unknown`) from patient absence (`absent`).
   - Queues 1 Hz monitoring snapshots and alert lifecycle updates to SQLite (`monitor_log` and `alerts` tables) via a background `DataRecorder` thread (5-second batch flush).
4. **Configurable Threshold & Alert Engine (`settings.py`, `alerts.py`):**
   - All alert rules start disabled (`None`) in `config.json` — no clinical thresholds are hardcoded.
   - Supports upper/lower bounds on **Sensor HR (`MAX30100`)**, **Sensor SpO2 (`MAX30100`)**, **Sensor motion / agitation (`MPU9250/6500/9255`, in `g`)**, and **Camera HR (`rPPG`)**, plus patient absence duration, sensor-vs-camera HR disagreement tolerance, and Arduino / `MAX30100` / `MPU` fault alerts.
5. **Non-Blocking SMTP Email Alerts (`notifier.py`):**
   - Sends alert emails on background threads using Python's standard `smtplib`, loading credentials exclusively from environment variables or a git-ignored `.env` file.
6. **Minimal Dashboard Integration (`app.py`):**
   - Added sidebar controls for Arduino port selection, baud rate, Connect / Disconnect / Refresh, data recording toggle, and an **Alert settings** form + **Send test email** button.
   - Added live **Sensor HR**, **Sensor SpO2***, and **Motion (MPU)** metric cards (`g` and `°/s`), presence status, and active alert banners in the right column.

---

## 2. List of Files Created or Modified

### Created Files
| File | Purpose |
|---|---|
| `Pulse-Vision/hardware.py` | Serial port discovery, `SerialReader` background thread, `MAX30100` + `MPU9250/6500/9255` NDJSON & legacy line parser, `MockSerial` simulator |
| `Pulse-Vision/settings.py` | Load/save/validate `config.json` alert rules and serial settings |
| `Pulse-Vision/monitor.py` | Unified `Snapshot`, `PresenceTracker`, and background `DataRecorder` |
| `Pulse-Vision/alerts.py` | `Alert` model and `AlertEngine` (confirmation, deduplication, cooldown, categories) |
| `Pulse-Vision/notifier.py` | Non-blocking SMTP email sender reading credentials from environment variables / `.env` |
| `Pulse-Vision/firmware/pulsevision_sensors/pulsevision_sensors.ino` | Arduino UNO R4 WiFi firmware for `MAX30100` + `MPU9250/6500/9255` outputting 1 Hz NDJSON |
| `Pulse-Vision/.env.example` | Example template for `PV_SMTP_*` environment variables |
| `Pulse-Vision/tests/test_hardware_integration.py` | 36 unit and integration tests covering Phase 8 scenarios (including MPU motion & fault alerts) |
| `Pulse-Vision/tests/test_app_smoke.py` | Headless Streamlit `AppTest` smoke test for the UI and mock serial connection |
| `Pulse-Vision/INTEGRATION_GUIDE.md` | This deliverable document |

### Modified Files
| File | Changes |
|---|---|
| `Pulse-Vision/app.py` | Added sidebar Arduino/alert controls, live `MAX30100` + `MPU` metrics & alert status panel, and 1 Hz `monitor_step` integration |
| `Pulse-Vision/database.py` | Added `monitor_log` (with `hw_mpu_ok`, `hw_accel_g`, `hw_motion_g`, `hw_gyro_dps`) and `alerts` tables while preserving `readings` |
| `Pulse-Vision/requirements.txt` | Added `pyserial` and `pytest` |
| `Pulse-Vision/.gitignore` | Ignored `.env`, `config.json`, `data/*.log`, and `.pytest_cache/` |
| `Pulse-Vision/README.md` | Documented dual-sensor hardware setup, serial JSON format, alert configuration, and test commands |

---

## 3. Arduino Firmware Changes

Created `Pulse-Vision/firmware/pulsevision_sensors/pulsevision_sensors.ino` for the **Arduino UNO R4 WiFi** (`arduino:renesas_uno:unor4wifi`):
- **Sensor 1 (`MAX30100`, I2C `0x57`):** Uses `MAX30100_PulseOximeter.h` (`MAX30100lib` v1.2.1). Calls `pox.update()` tightly in `loop()` and reports `hr_bpm` and `spo2_pct` (`null` if no beat within 3 seconds).
- **Sensor 2 (`MPU9250 / MPU6500 / MPU9255`, I2C `0x68` or `0x69`):** Uses `MPU9250_asukiaaa.h` (`MPU9250_asukiaaa` v1.5.13). Auto-probes `0x68` (`AD0 LOW`) and `0x69` (`AD0 HIGH`), samples acceleration at 20 Hz to capture peak dynamic motion `motion_g = |accelSqrt() - 1.0g|` over each 1-second window, and reports `ax`, `ay`, `az`, `a_g`, `motion_g`, `gx`, `gy`, `gz`.
- **Fault Recovery:** Both sensors independently retry initialization every 5 seconds if disconnected or if an I2C read fails.

---

## 4. Exact Python Packages to Install

From inside `c:\Users\aryan\Downloads\iotbdmproject\Pulse-Vision`:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Contents of `requirements.txt`:
```text
mediapipe>=0.10.20
opencv-contrib-python
numpy
scipy
streamlit
pandas
pyserial
pytest
```

---

## 5. Exact Commands to Run the Application

```powershell
cd c:\Users\aryan\Downloads\iotbdmproject\Pulse-Vision
.\.venv\Scripts\streamlit run app.py
```

Open **http://localhost:8501** in a single browser tab.

---

## 6. Instructions for Selecting the Arduino COM Port

1. Plug the **Arduino UNO R4 WiFi** into the laptop via USB (and close the Arduino IDE Serial Monitor so it does not lock the port).
2. In the PulseVision dashboard left sidebar under **Arduino**, click **Refresh**.
3. Open the **Serial port** dropdown:
   - Any detected Arduino port (USB VID `0x2341`, e.g., `COM5 - Arduino UNO R4 WiFi`) appears at the top of the list.
   - To test without hardware, select **`MOCK (simulated)`**.
4. Keep **Baud rate** at `115200` and click **Connect**.

---

## 7. Instructions for Connecting & Testing the Hardware

### Wiring (`MAX30100` + `MPU9250/6500/9255` to Arduino UNO R4 WiFi)
Both sensors share the I2C bus (`A4` SDA / `A5` SCL) at `3.3V`:
- **`MAX30100` (`0x57`):** `VIN -> 3.3V`, `GND -> GND`, `SDA -> A4`, `SCL -> A5`
- **`MPU9250 / MPU6500 / MPU9255` (`0x68` or `0x69`):** `VCC -> 3.3V`, `GND -> GND`, `SDA -> A4`, `SCL -> A5` (`AD0 -> GND` for `0x68` or `3.3V` for `0x69`; firmware auto-detects either)

### Compiling & Uploading the Firmware
Using the workspace `arduino-cli.exe` (replace `COM5` with your port):

```powershell
cd c:\Users\aryan\Downloads\iotbdmproject
.\arduinoide\bin\arduino-cli.exe compile --fqbn arduino:renesas_uno:unor4wifi Pulse-Vision\firmware\pulsevision_sensors
.\arduinoide\bin\arduino-cli.exe upload -p COM5 --fqbn arduino:renesas_uno:unor4wifi Pulse-Vision\firmware\pulsevision_sensors
```

---

## 8. Example Serial Data Format

Newline-delimited JSON at `115200` baud (1 line per second):

```json
{"t_ms":12345,"max30100":{"ok":true,"hr_bpm":72.4,"spo2_pct":97},"mpu":{"ok":true,"ax":-0.02,"ay":0.01,"az":0.99,"a_g":1.00,"motion_g":0.02,"gx":0.4,"gy":-0.2,"gz":0.1}}
{"t_ms":13345,"max30100":{"ok":true,"hr_bpm":null,"spo2_pct":null},"mpu":{"ok":true,"ax":0.12,"ay":-0.45,"az":1.52,"a_g":1.59,"motion_g":0.84,"gx":42.1,"gy":-18.5,"gz":9.0}}
{"t_ms":14345,"max30100":{"ok":false,"hr_bpm":null,"spo2_pct":null},"mpu":{"ok":false,"ax":null,"ay":null,"az":null,"a_g":null,"motion_g":null,"gx":null,"gy":null,"gz":null}}
```

---

## 9. Instructions for Configuring Thresholds & Email Recipient

### Step 1 — Configure SMTP Credentials (`.env`)
Copy `Pulse-Vision/.env.example` to `Pulse-Vision/.env` and fill in your SMTP details:

```ini
PV_SMTP_HOST=smtp.gmail.com
PV_SMTP_PORT=587
PV_SMTP_SECURITY=starttls
PV_SMTP_USER=your.address@gmail.com
PV_SMTP_PASSWORD=your_16_char_app_password
PV_SMTP_FROM=your.address@gmail.com
```

### Step 2 — Configure Thresholds & Recipient in the UI
1. In the left sidebar, open the **Alert settings** expander.
2. Enter desired **Low** and/or **High** thresholds for:
   - **Sensor heart rate (MAX30100)** (`20–250 BPM`)
   - **Sensor SpO2 (MAX30100, uncalibrated)** (`50–100 %`)
   - **Sensor motion / agitation (MPU9250/6500/9255)** (`0.0–16.0 g`, e.g., High `0.5 g` for restlessness/agitation or `1.5 g` for sudden impact/fall)
   - **Camera heart rate estimate (rPPG)** (`40–180 BPM`)
   *(Leave any field blank to keep that rule disabled.)*
3. Set **Alert when patient not detected for (s)**, **Alert when sensor and camera HR differ by more than (BPM)**, and optionally enable **Alert on Arduino disconnection / stale data**.
4. Tick **Send email alerts**, enter the **Recipient email**, and click **Save settings**.

---

## 10. Test Results & Remaining Limitations

### Test Suite Results
Run from `c:\Users\aryan\Downloads\iotbdmproject\Pulse-Vision`:
```powershell
.\.venv\Scripts\python -m pytest -q
```
- **Result:** `37 passed` (`test_hardware_integration.py` + `test_app_smoke.py`).
- **Firmware Build:** Verified clean compilation for `arduino:renesas_uno:unor4wifi` (`68316 bytes (26%)` flash, `9284 bytes (28%)` RAM).

### Remaining Limitations
- **Uncalibrated SpO2:** `MAX30100lib` SpO2 is not medically calibrated and is labeled as an uncalibrated estimate across the UI and alert emails.
- **Face-Based Presence:** Presence tracking relies on MediaPipe Face Landmarker; if a patient lies turned away from the camera for longer than the configured absence duration, an absence alert will trigger.
