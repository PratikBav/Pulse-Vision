"""Alert rules: thresholds, bed presence, camera/sensor disagreement and hardware faults.

Categories keep a real measurement problem separate from an equipment problem:
  THRESHOLD     - a measured value crossed a user-configured limit
  PRESENCE      - the patient has not been seen for the configured duration
  DISAGREEMENT  - sensor and camera heart rates differ by more than the tolerance
  SENSOR_FAULT  - Arduino disconnected, readings stale, or sensor reports an error

Missing readings never trigger a THRESHOLD alert - they can only show up as a
SENSOR_FAULT (if enabled). Each alert notifies once when it starts; it must
clear before it can alert again, and the cooldown stops a flapping condition
from sending repeated emails.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from settings import METRICS

THRESHOLD = "THRESHOLD"
PRESENCE = "PRESENCE"
DISAGREEMENT = "DISAGREEMENT"
SENSOR_FAULT = "SENSOR_FAULT"


@dataclass
class Alert:
    key: str
    category: str
    message: str
    started_at: float
    readings: dict
    session_id: str | None = None
    simulated: bool = False
    notify: bool = True
    delivery_status: str = "pending"
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def timestamp(self):
        return datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")

    def to_row(self):
        return {
            "uid": self.uid, "session_id": self.session_id, "raised_at": self.timestamp,
            "alert_key": self.key, "category": self.category, "message": self.message,
            "readings_json": json.dumps(self.readings), "simulated": int(self.simulated),
            "delivery_status": self.delivery_status,
        }


def _fmt(value, unit, decimals=0):
    if value is None:
        return "unavailable"
    return f"{value:.{decimals}f} {unit}"


def relevant_readings(snap):
    """The values included in alert records and emails (labelled by source)."""
    return {
        "Sensor heart rate (MAX30100)": _fmt(snap.hw_hr_bpm, "BPM"),
        "Sensor SpO2 (MAX30100, uncalibrated)": _fmt(snap.hw_spo2_pct, "%"),
        "Sensor motion (MPU9250/6500/9255)": _fmt(snap.hw_motion_g, "g", decimals=2),
        "Sensor acceleration (MPU9250/6500/9255)": _fmt(snap.hw_accel_g, "g", decimals=2),
        "Skin surface temperature (MLX90614, NOT core body temp)": _fmt(snap.hw_object_temp_c, "°C", decimals=1),
        "Ambient temperature (MLX90614)": _fmt(snap.hw_ambient_temp_c, "°C", decimals=1),
        "Camera heart rate estimate": _fmt(snap.cam_bpm, "BPM"),
        "Camera signal quality": snap.cam_quality or "unavailable",
        "Patient presence": snap.presence,
        "Arduino status": snap.hw_status + (" (stale)" if snap.hw_stale else ""),
        "Camera status": snap.camera_status,
        "Data source": "SIMULATED (mock serial)" if snap.hw_simulated else "Arduino",
    }


def _fmt_val(value, unit):
    if unit == "g":
        return f"{value:.2f} {unit}"
    if unit == "°C":
        return f"{value:.1f} {unit}"
    return f"{value:.0f} {unit}"


def current_conditions(snap, config, hardware_expected):
    """Return {key: (category, message, needs_confirmation)} for every condition true right now."""
    found = {}

    for metric, meta in METRICS.items():
        value = getattr(snap, metric)
        if value is None:
            continue  # missing data is not a threshold breach
        rule = config["thresholds"].get(metric, {})
        high, low = rule.get("high"), rule.get("low")
        unit = meta["unit"]
        if high is not None and value > high:
            found[f"{metric}_high"] = (THRESHOLD, f"{meta['label']} is {_fmt_val(value, unit)}, "
                                       f"above the configured limit of {high} {unit}", True)
        if low is not None and value < low:
            found[f"{metric}_low"] = (THRESHOLD, f"{meta['label']} is {_fmt_val(value, unit)}, "
                                      f"below the configured limit of {low} {unit}", True)

    absence = config["presence"].get("absence_seconds")
    if absence and snap.presence == "absent" and snap.absent_seconds >= absence:
        found["patient_absent"] = (PRESENCE, f"Patient not detected for {snap.absent_seconds:.0f} s "
                                   f"(configured limit {absence:.0f} s)", False)

    tolerance = config.get("disagreement_bpm")
    if (tolerance and snap.hw_hr_bpm is not None and snap.cam_bpm is not None
            and snap.cam_quality in ("Good", "Fair")):
        diff = abs(snap.hw_hr_bpm - snap.cam_bpm)
        if diff > tolerance:
            found["hr_disagreement"] = (DISAGREEMENT, f"Sensor ({snap.hw_hr_bpm:.0f} BPM) and camera "
                                        f"({snap.cam_bpm:.0f} BPM) heart rates differ by {diff:.0f} BPM "
                                        f"(tolerance {tolerance} BPM)", True)

    if config.get("hardware_fault_alerts") and hardware_expected:
        if snap.hw_status != "connected":
            found["hw_disconnected"] = (SENSOR_FAULT, f"Arduino is not connected (status: {snap.hw_status})", True)
        elif snap.hw_stale:
            found["hw_stale"] = (SENSOR_FAULT, "No valid Arduino reading received recently (data stale)", True)
        else:
            if snap.hw_sensor_ok is False:
                found["hw_sensor_error"] = (SENSOR_FAULT, "MAX30100 sensor reports an error", True)
            if snap.hw_mpu_ok is False:
                found["hw_mpu_error"] = (SENSOR_FAULT, "MPU9250/6500/9255 motion sensor reports an error", True)
            if snap.hw_mlx_ok is False:
                found["hw_mlx_error"] = (SENSOR_FAULT, "MLX90614 temperature sensor reports an error — check wiring", True)
    return found


class AlertEngine:
    """Tracks which conditions are active and decides when to notify."""

    def __init__(self):
        self.pending = {}         # key -> time first seen (waiting for confirmation)
        self.active = {}          # key -> Alert
        self.last_notified = {}   # key -> time of last notification

    def evaluate(self, snap, config, hardware_expected=True):
        """Update state from one snapshot.

        Returns (new_alerts, cleared_alerts). Every new Alert has .notify set to
        False if it falls inside the cooldown of an earlier alert of the same kind.
        """
        now = snap.time
        conditions = current_conditions(snap, config, hardware_expected)
        confirm = float(config.get("confirm_seconds") or 0)
        cooldown = float(config.get("cooldown_seconds") or 0)

        new_alerts = []
        for key, (category, message, needs_confirm) in conditions.items():
            if key in self.active:
                continue
            first_seen = self.pending.setdefault(key, now)
            if needs_confirm and now - first_seen < confirm:
                continue
            alert = Alert(key=key, category=category, message=message, started_at=now,
                          readings=relevant_readings(snap), session_id=snap.session_id,
                          simulated=snap.hw_simulated)
            last = self.last_notified.get(key)
            if last is not None and now - last < cooldown:
                alert.notify = False
                alert.delivery_status = "suppressed (cooldown)"
            else:
                self.last_notified[key] = now
            self.active[key] = alert
            new_alerts.append(alert)

        cleared = []
        for key in list(self.pending):
            if key not in conditions:
                del self.pending[key]
        for key in list(self.active):
            if key not in conditions:
                cleared.append(self.active.pop(key))
        return new_alerts, cleared

    def active_keys(self):
        return sorted(self.active)

    def reset(self):
        """Forget active/pending conditions (cooldown history is kept)."""
        self.pending.clear()
        self.active.clear()
