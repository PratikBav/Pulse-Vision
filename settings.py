"""Monitoring settings: serial port, alert rules and email recipient.

Stored in config.json next to the app. Every alert rule starts DISABLED with no
threshold - thresholds must be chosen by the user (e.g. with clinical guidance);
none are hardcoded.
"""

import copy
import json
import logging
import os

log = logging.getLogger("pulsevision.settings")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# Measurements that threshold rules can watch.
# valid_range = what the sensor/method can actually report; thresholds outside it are rejected.
METRICS = {
    "hw_hr_bpm": {"label": "Sensor heart rate (MAX30100)", "unit": "BPM", "valid_range": (20, 250)},
    "hw_spo2_pct": {"label": "Sensor SpO2 (MAX30100, uncalibrated)", "unit": "%", "valid_range": (50, 100)},
    "hw_motion_g": {"label": "Sensor motion / agitation (MPU9250/6500/9255)", "unit": "g", "valid_range": (0.0, 16.0)},
    "cam_bpm": {"label": "Camera heart rate estimate (rPPG)", "unit": "BPM", "valid_range": (40, 180)},
    # MLX90614 skin surface temperature - NOT core body temperature; alert disabled by default.
    "hw_object_temp_c": {
        "label": "Skin surface temperature (MLX90614) — NOT core body temperature",
        "unit": "°C",
        "valid_range": (20.0, 55.0),
    },
}

DEFAULT_CONFIG = {
    "serial": {
        "port": "auto",          # "auto" = first Arduino found, or a name such as "COM5"
        "baud": 115200,
        "stale_seconds": 5.0,    # no valid reading for this long = stale
    },
    "thresholds": {key: {"low": None, "high": None} for key in METRICS},
    "presence": {
        "absence_seconds": None,  # None = absence alert disabled
    },
    "disagreement_bpm": None,     # None = camera vs sensor comparison disabled
    "hardware_fault_alerts": False,
    "confirm_seconds": 3.0,       # a condition must last this long before it alerts
    "cooldown_seconds": 300.0,    # minimum time between two alerts of the same kind
    "email": {
        "enabled": False,
        "recipient": "",
    },
    "notifications": {
        "enabled": False,
        "patient_id": "Unassigned",
        "channel": "push",
    },
}


def _merge(defaults, values):
    """Recursively fill missing keys in values from defaults."""
    result = copy.deepcopy(defaults)
    for key, value in (values or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path=CONFIG_PATH):
    """Load config.json, falling back to defaults if it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return _merge(DEFAULT_CONFIG, json.load(f))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_CONFIG)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read %s (%s) - using defaults", path, exc)
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config, path=CONFIG_PATH):
    """Validate and save the config. Returns a list of error messages (empty on success)."""
    errors = validate_config(config)
    if errors:
        return errors
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)
    return []


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def validate_config(config):
    """Return a list of human-readable problems with the config."""
    errors = []
    for key, meta in METRICS.items():
        rule = config["thresholds"].get(key, {})
        low, high = rule.get("low"), rule.get("high")
        lo_valid, hi_valid = meta["valid_range"]
        for name, value in (("low", low), ("high", high)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{meta['label']}: {name} threshold must be a number")
            elif not lo_valid <= value <= hi_valid:
                errors.append(f"{meta['label']}: {name} threshold {value} {meta['unit']} is outside "
                              f"what this measurement can report ({lo_valid}-{hi_valid} {meta['unit']})")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low >= high:
            errors.append(f"{meta['label']}: low threshold must be below high threshold")

    absence = config["presence"].get("absence_seconds")
    if absence is not None and not _positive(absence):
        errors.append("Absence duration must be a positive number of seconds")
    tolerance = config.get("disagreement_bpm")
    if tolerance is not None and not _positive(tolerance):
        errors.append("Camera/sensor disagreement tolerance must be a positive number of BPM")
    for key in ("confirm_seconds", "cooldown_seconds"):
        if not (_positive(config.get(key)) or config.get(key) == 0):
            errors.append(f"{key} must be zero or a positive number")
    if not _positive(config["serial"].get("stale_seconds")):
        errors.append("Stale timeout must be a positive number of seconds")
    if not _positive(config["serial"].get("baud")):
        errors.append("Baud rate must be a positive number")

    email = config["email"]
    if email.get("enabled") and "@" not in (email.get("recipient") or ""):
        errors.append("Email alerts are enabled but the recipient address is missing or invalid")
    notification = config.get("notifications", {})
    if notification.get("channel", "push") not in ("push", "sms", "both"):
        errors.append("Notification channel must be push, sms, or both")
    patient_id = notification.get("patient_id", "Unassigned")
    if not isinstance(patient_id, str) or not patient_id.strip() or "/" in patient_id:
        errors.append("Patient identifier must be a non-empty string without '/'")
    return errors
