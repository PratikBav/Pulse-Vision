"""Unified monitoring state, bed-presence tracking and background data recording.

The camera loop in app.py builds one Snapshot per second from:
  - the latest Arduino reading (hardware.SerialReader), and
  - the latest camera results (face detected, rPPG heart rate, quality).
Hardware measurements and camera estimates are kept in separate fields.
"""

import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import database

log = logging.getLogger("pulsevision.monitor")


@dataclass
class Snapshot:
    time: float                      # laptop time.time()
    timestamp: str                   # ISO time, for storage and emails
    session_id: str | None = None
    # ---- hardware (Arduino) - None when unavailable or stale, never guessed ----
    hw_status: str = "disconnected"
    hw_port: str | None = None
    hw_hr_bpm: float | None = None
    hw_spo2_pct: float | None = None
    hw_sensor_ok: bool | None = None
    hw_mpu_ok: bool | None = None
    hw_accel_g: float | None = None
    hw_motion_g: float | None = None
    hw_gyro_dps: float | None = None
    hw_last_received: float | None = None
    hw_age_s: float | None = None
    hw_stale: bool = False
    hw_simulated: bool = False
    hw_format: str | None = None
    sensor2: dict = field(default_factory=dict)
    # MLX90614 skin surface temperature (NOT core body temperature)
    hw_mlx_ok: bool | None = None
    hw_ambient_temp_c: float | None = None
    hw_object_temp_c: float | None = None
    # ---- camera (estimates) ----
    camera_status: str = "off"       # "ok", "unavailable" or "off"
    cam_bpm: float | None = None
    cam_quality: str | None = None
    face_detected: bool | None = None
    presence: str = "unknown"        # "present", "absent" or "unknown"
    absent_seconds: float = 0.0
    # ---- alerts ----
    active_alerts: list = field(default_factory=list)


class PresenceTracker:
    """Turns per-frame face detection into a patient-present / absent status.

    A single missed frame never counts as absence: absent_seconds grows from the
    last time the face was seen, and the alert rule compares it with a
    configurable duration. If the camera is unavailable, presence is "unknown"
    (not "absent"), so a camera fault never looks like the patient leaving.
    """

    def __init__(self):
        self.last_seen = None
        self.absent_since = None

    def update(self, face_detected, now):
        if face_detected is None:  # camera unavailable
            self.absent_since = None
            return "unknown", 0.0
        if face_detected:
            self.last_seen = now
            self.absent_since = None
            return "present", 0.0
        if self.absent_since is None:
            self.absent_since = self.last_seen if self.last_seen is not None else now
        return "absent", now - self.absent_since

    def reset(self):
        self.last_seen = None
        self.absent_since = None


def build_snapshot(hw_state, stale_seconds, now=None, session_id=None, camera_status="off",
                   cam_bpm=None, cam_quality=None, face_detected=None, presence="unknown",
                   absent_seconds=0.0):
    """Combine the serial reader state and camera results into one Snapshot."""
    now = time.time() if now is None else now
    snap = Snapshot(
        time=now, timestamp=datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        session_id=session_id, hw_status=hw_state["status"], hw_port=hw_state["port"],
        camera_status=camera_status, cam_bpm=cam_bpm, cam_quality=cam_quality,
        face_detected=face_detected, presence=presence, absent_seconds=absent_seconds,
    )
    reading = hw_state.get("latest")
    if reading is not None:
        snap.hw_last_received = reading.received_at
        snap.hw_age_s = max(0.0, now - reading.received_at)
        snap.hw_stale = snap.hw_age_s > stale_seconds
        snap.hw_simulated = reading.simulated
        snap.hw_format = reading.source_format
        if not snap.hw_stale and snap.hw_status == "connected":
            snap.hw_hr_bpm = reading.hr_bpm
            snap.hw_spo2_pct = reading.spo2_pct
            snap.hw_sensor_ok = reading.sensor_ok
            snap.hw_mpu_ok = reading.mpu_ok
            snap.hw_accel_g = reading.accel_g
            snap.hw_motion_g = reading.motion_g
            snap.hw_gyro_dps = reading.gyro_dps
            snap.sensor2 = dict(reading.sensor2)
            snap.hw_mlx_ok = reading.mlx_ok
            snap.hw_ambient_temp_c = reading.ambient_temp_c
            snap.hw_object_temp_c = reading.object_temp_c
    elif snap.hw_status == "connected":
        snap.hw_stale = True  # connected, but no valid reading has arrived yet
    return snap


class DataRecorder:
    """Writes snapshots and alerts to SQLite from a background thread.

    The camera loop only puts items on a queue, so slow disk writes never cause
    frame gaps. Storage errors are logged and shown in the UI, never raised.
    """

    FLUSH_SECONDS = 5.0

    def __init__(self):
        self._queue = queue.Queue(maxsize=10000)
        self.rows_written = 0
        self.alerts_written = 0
        self.last_error = None
        self.last_write = None
        self._thread = threading.Thread(target=self._run, name="data-recorder", daemon=True)
        self._thread.start()

    def log_snapshot(self, snap):
        row = {
            "timestamp": snap.timestamp, "session_id": snap.session_id,
            "hw_status": snap.hw_status, "hw_port": snap.hw_port, "hw_simulated": int(snap.hw_simulated),
            "hw_hr_bpm": snap.hw_hr_bpm, "hw_spo2_pct": snap.hw_spo2_pct,
            "hw_sensor_ok": None if snap.hw_sensor_ok is None else int(snap.hw_sensor_ok),
            "hw_mpu_ok": None if snap.hw_mpu_ok is None else int(snap.hw_mpu_ok),
            "hw_accel_g": snap.hw_accel_g, "hw_motion_g": snap.hw_motion_g,
            "hw_gyro_dps": snap.hw_gyro_dps,
            "hw_age_s": None if snap.hw_age_s is None else round(snap.hw_age_s, 2),
            "sensor2_json": json.dumps(snap.sensor2) if snap.sensor2 else None,
            "hw_mlx_ok": None if snap.hw_mlx_ok is None else int(snap.hw_mlx_ok),
            "hw_ambient_temp_c": snap.hw_ambient_temp_c,
            "hw_object_temp_c": snap.hw_object_temp_c,
            "camera_status": snap.camera_status, "cam_bpm": snap.cam_bpm, "cam_quality": snap.cam_quality,
            "face_detected": None if snap.face_detected is None else int(snap.face_detected),
            "presence": snap.presence, "absent_seconds": round(snap.absent_seconds, 1),
            "active_alerts": ",".join(snap.active_alerts) or None,
        }
        self._put(("snapshot", row))

    def record_alert(self, alert):
        self._put(("alert", alert.to_row()))

    def update_alert(self, uid, delivery_status=None, cleared_at=None):
        self._put(("alert_update", (uid, delivery_status, cleared_at)))

    def _put(self, item):
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.last_error = "Recording queue full - some records were dropped"
            log.error(self.last_error)

    def status(self):
        return {"rows_written": self.rows_written, "alerts_written": self.alerts_written,
                "last_error": self.last_error, "last_write": self.last_write,
                "pending": self._queue.qsize()}

    def flush(self, timeout=5.0):
        """Wait until everything queued so far has been written (used by tests)."""
        end = time.time() + timeout
        while (self._queue.unfinished_tasks > 0) and time.time() < end:
            time.sleep(0.05)

    def _run(self):
        while True:
            items = [self._queue.get()]
            deadline = time.time() + self.FLUSH_SECONDS
            # Batch snapshot rows so the disk is written every few seconds, not every second
            while items[-1][0] == "snapshot" and time.time() < deadline:
                try:
                    items.append(self._queue.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            try:
                self._write(items)
                self.last_write = time.time()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - storage must never crash monitoring
                self.last_error = f"Storage error: {exc}"
                log.error(self.last_error)
            finally:
                for _ in items:
                    self._queue.task_done()

    def _write(self, items):
        rows = [data for kind, data in items if kind == "snapshot"]
        if rows:
            database.insert_monitor_rows(rows)
            self.rows_written += len(rows)
        for kind, data in items:
            if kind == "alert":
                database.insert_alert(data)
                self.alerts_written += 1
            elif kind == "alert_update":
                database.update_alert(*data)


def snapshot_dict(snap):
    return asdict(snap)
