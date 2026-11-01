"""Alerts: warn when the patient is not detected, or the heart rate is high or changes sharply.

AlertMonitor decides *when* to alert. Each Alert records the exact condition that
fired and the readings at that moment. PulseVision shows it as a pop-up and saves it
to the database, where the Notification app (notification_app.py) picks it up.

These alerts are based on an *estimated* heart rate. They are not medical alarms.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

ABSENCE_SECONDS = 10               # no face for this long -> "patient not present"
FLUCTUATION_WINDOW_SECONDS = 30    # look at the shown heart rate over this period...
FLUCTUATION_BPM = 25               # ...and alert if it changes by more than this
FLUCTUATION_COOLDOWN_SECONDS = 60  # do not repeat the fluctuation alert more often than this
HIGH_BPM_DEFAULT = 120             # default "high heart rate" threshold (can be changed in the dashboard)
HIGH_BPM_SECONDS = 10              # the heart rate must stay above the threshold this long

ABSENT = "Patient not present"
FLUCTUATION = "Heart rate fluctuation"
HIGH_HEART_RATE = "High heart rate"


@dataclass
class Alert:
    kind: str              # ABSENT, FLUCTUATION or HIGH_HEART_RATE
    message: str           # short message for the pop-up
    condition: str         # the rule that fired, with its limit
    details: str           # the readings at the moment the alert fired
    bpm: float = None      # current (or last known) heart rate
    quality: str = None    # signal quality of that heart rate
    quiet: bool = False    # quiet alerts are only shown as a small pop-up


def clock(seconds_ago=0.0):
    """Wall-clock time, seconds_ago seconds before now, as HH:MM:SS."""
    return f"{datetime.now() - timedelta(seconds=seconds_ago):%H:%M:%S}"


class AlertMonitor:
    """Watches the face status and the shown heart rate, and returns Alerts when needed."""

    def __init__(self):
        self.face_missing_since = None
        self.absence_alerted = False
        self.recent_bpm = deque()  # (time in seconds, bpm) of shown heart rates
        self.last_fluctuation_time = None
        self.high_since = None     # when the heart rate first went above the threshold
        self.high_values = []      # heart rates recorded while above the threshold
        self.high_alerted = False
        self.last_bpm = None       # last recorded heart rate, its quality and time
        self.last_quality = None
        self.last_bpm_time = None

    def update_face(self, now, face_present):
        """Call every frame. Returns an Alert once per absence that lasts ABSENCE_SECONDS."""
        if face_present:
            self.face_missing_since = None
            self.absence_alerted = False
            return None

        # The measurement restarts when the face returns, so old heart rates no longer apply
        self.recent_bpm.clear()
        self.high_since = None
        self.high_values = []
        self.high_alerted = False
        if self.face_missing_since is None:
            self.face_missing_since = now
        absent_for = now - self.face_missing_since
        if not self.absence_alerted and absent_for >= ABSENCE_SECONDS:
            self.absence_alerted = True
            details = f"Patient not detected since {clock(absent_for)} ({absent_for:.0f} s)."
            if self.last_bpm is not None:
                details += (f" Last heart rate: {self.last_bpm:.0f} BPM ({self.last_quality} signal) "
                            f"at {clock(now - self.last_bpm_time)}.")
            else:
                details += " No heart rate was measured before the patient left."
            return Alert(
                ABSENT,
                "The patient is not detected in front of the camera.",
                condition=f"No face detected for {ABSENCE_SECONDS} s (limit: {ABSENCE_SECONDS} s)",
                details=details,
                bpm=self.last_bpm,
                quality=self.last_quality,
            )
        return None

    def is_absent(self):
        """True while a 'patient not present' alert is active."""
        return self.absence_alerted

    def update_heart_rate(self, now, bpm, high_threshold=HIGH_BPM_DEFAULT, quality=None):
        """Call whenever a new heart rate is recorded. Returns a list of Alerts (often empty)."""
        alerts = []
        self.last_bpm, self.last_quality, self.last_bpm_time = bpm, quality, now

        # High heart rate: above the threshold for HIGH_BPM_SECONDS in a row
        if bpm > high_threshold:
            if self.high_since is None:
                self.high_since = now
                self.high_values = []
            self.high_values.append(bpm)
            above_for = now - self.high_since
            if not self.high_alerted and above_for >= HIGH_BPM_SECONDS:
                self.high_alerted = True
                alerts.append(Alert(
                    HIGH_HEART_RATE,
                    f"High heart rate: {bpm:.0f} BPM (limit {high_threshold:.0f} BPM).",
                    condition=f"Heart rate above {high_threshold:.0f} BPM for {HIGH_BPM_SECONDS} s",
                    details=(f"Current heart rate {bpm:.0f} BPM ({quality} signal). Above the limit since "
                             f"{clock(above_for)} ({above_for:.0f} s): lowest {min(self.high_values):.0f}, "
                             f"highest {max(self.high_values):.0f} BPM."),
                    bpm=bpm,
                    quality=quality,
                ))
        else:
            self.high_since = None
            self.high_values = []
            self.high_alerted = False

        # Fluctuation: the heart rate changed by more than FLUCTUATION_BPM within the window
        self.recent_bpm.append((now, bpm))
        while now - self.recent_bpm[0][0] > FLUCTUATION_WINDOW_SECONDS:
            self.recent_bpm.popleft()
        values = [b for _, b in self.recent_bpm]
        change = max(values) - min(values)
        cooled_down = (self.last_fluctuation_time is None
                       or now - self.last_fluctuation_time >= FLUCTUATION_COOLDOWN_SECONDS)
        if change > FLUCTUATION_BPM and cooled_down:
            self.last_fluctuation_time = now
            alerts.append(Alert(
                FLUCTUATION,
                f"Heart rate changed by {change:.0f} BPM within {FLUCTUATION_WINDOW_SECONDS} s.",
                condition=f"Change of more than {FLUCTUATION_BPM} BPM within {FLUCTUATION_WINDOW_SECONDS} s",
                details=(f"Heart rate ranged from {min(values):.0f} to {max(values):.0f} BPM; "
                         f"now {bpm:.0f} BPM ({quality} signal). Please check the patient and the measurement."),
                bpm=bpm,
                quality=quality,
                quiet=True,
            ))
        return alerts
