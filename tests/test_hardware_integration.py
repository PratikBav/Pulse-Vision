"""Tests for the hardware integration. Run with:  python -m pytest -q"""

import os
import sys
import threading
import time

import numpy as np
import pytest
import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from alerts import PRESENCE, SENSOR_FAULT, THRESHOLD, Alert, AlertEngine  # noqa: E402
from hardware import MOCK_PORT, MalformedLine, MockSerial, SerialReader, parse_line  # noqa: E402
from monitor import DataRecorder, PresenceTracker, build_snapshot  # noqa: E402
from notifier import EmailNotifier, build_message  # noqa: E402
from settings import DEFAULT_CONFIG, _merge, validate_config  # noqa: E402


def cfg(**overrides):
    return _merge(DEFAULT_CONFIG, overrides)


def wait_for(condition, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- parsing
def test_parse_valid_json():
    line = ('{"t_ms":1000,"max30100":{"ok":true,"hr_bpm":72.5,"spo2_pct":97},'
            '"mpu":{"ok":true,"ax":0.0,"ay":0.0,"az":1.04,"a_g":1.04,"motion_g":0.04,"gx":3.0,"gy":4.0,"gz":0.0}}')
    r = parse_line(line, received_at=5.0)
    assert (r.device_ms, r.hr_bpm, r.spo2_pct, r.sensor_ok, r.simulated) == (1000, 72.5, 97.0, True, False)
    assert (r.mpu_ok, r.accel_g, r.motion_g, r.gyro_dps) == (True, 1.04, 0.04, 5.0)
    assert (r.mlx_ok, r.ambient_temp_c, r.object_temp_c) == (None, None, None)


def test_parse_mlx_valid_json():
    line = ('{"t_ms":2000,"max30100":{"ok":true,"hr_bpm":70,"spo2_pct":98},'
            '"mpu":{"ok":true,"motion_g":0.01},'
            '"mlx90614":{"ok":true,"ambient_temp_c":29.5,"object_temp_c":33.8}}')
    r = parse_line(line, received_at=10.0)
    assert (r.mlx_ok, r.ambient_temp_c, r.object_temp_c) == (True, 29.5, 33.8)


def test_parse_mlx_backwards_compatible_without_mlx():
    line = '{"t_ms":1000,"max30100":{"ok":true,"hr_bpm":72,"spo2_pct":97},"mpu":{"ok":true,"motion_g":0.02}}'
    r = parse_line(line, received_at=5.0)
    assert r.mlx_ok is None
    assert r.ambient_temp_c is None
    assert r.object_temp_c is None


def test_parse_mlx_sensor_fault_discards_readings():
    line = '{"max30100":{"ok":true},"mlx90614":{"ok":false,"ambient_temp_c":29.5,"object_temp_c":33.8}}'
    r = parse_line(line)
    assert r.mlx_ok is False
    assert r.ambient_temp_c is None
    assert r.object_temp_c is None


def test_parse_mlx_missing_or_null_values():
    line = '{"mlx90614":{"ok":true,"ambient_temp_c":null,"object_temp_c":34.1}}'
    r = parse_line(line)
    assert r.mlx_ok is True
    assert r.ambient_temp_c is None
    assert r.object_temp_c == 34.1


@pytest.mark.parametrize("line", [
    '{"mlx90614":{"ok":true,"ambient_temp_c":999.0,"object_temp_c":34.0}}',
    '{"mlx90614":{"ok":true,"ambient_temp_c":28.0,"object_temp_c":300.0}}',
    '{"mlx90614":{"ok":"true"}}',
    '{"mlx90614":"not_an_object"}',
])
def test_parse_mlx_rejects_malformed(line):
    with pytest.raises(MalformedLine):
        parse_line(line)


def test_mlx_threshold_and_fault_alerts():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_object_temp_c": {"low": 30.0, "high": 37.0}},
                 hardware_fault_alerts=True, confirm_seconds=0)
    r_high = parse_line('{"mlx90614":{"ok":true,"ambient_temp_c":28.0,"object_temp_c":38.5}}')
    snap = build_snapshot({"status": "connected", "port": "COM5", "latest": r_high}, 5.0, now=1000.0)
    new, _ = engine.evaluate(snap, config)
    assert [(a.key, a.category) for a in new] == [("hw_object_temp_c_high", THRESHOLD)]
    assert "38.5 °C" in new[0].message
    assert "NOT core body temperature" in new[0].message

    # Fault alert when MLX sensor reports error
    engine_fault = AlertEngine()
    r_fault = parse_line('{"mlx90614":{"ok":false}}')
    snap_fault = build_snapshot({"status": "connected", "port": "COM5", "latest": r_fault}, 5.0, now=1000.0)
    new_fault, _ = engine_fault.evaluate(snap_fault, config)
    assert [(a.key, a.category) for a in new_fault] == [("hw_mlx_error", SENSOR_FAULT)]


def test_recorder_persists_temperature_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "mlx_test.db"))
    recorder = DataRecorder()
    recorder.FLUSH_SECONDS = 0.1
    r = parse_line('{"max30100":{"ok":true,"hr_bpm":72,"spo2_pct":98},'
                   '"mpu":{"ok":true,"motion_g":0.02},'
                   '"mlx90614":{"ok":true,"ambient_temp_c":29.1,"object_temp_c":33.7}}')
    snap = build_snapshot({"status": "connected", "port": "COM5", "latest": r}, 5.0, session_id="T1", now=1000.0)
    recorder.log_snapshot(snap)
    recorder.flush(timeout=10)

    df = database.load_monitor_log("T1")
    assert len(df) == 1
    assert df["hw_mlx_ok"].iloc[0] == 1
    assert df["hw_ambient_temp_c"].iloc[0] == pytest.approx(29.1)
    assert df["hw_object_temp_c"].iloc[0] == pytest.approx(33.7)


def test_mpu_motion_threshold_and_fault_alerts():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_motion_g": {"low": None, "high": 0.5}},
                 hardware_fault_alerts=True, confirm_seconds=0)
    r_high = parse_line('{"max30100":{"ok":true},"mpu":{"ok":true,"motion_g":0.85}}', received_at=1000.0)
    snap = build_snapshot({"status": "connected", "port": "COM5", "latest": r_high}, 5.0, now=1000.0)
    new, _ = engine.evaluate(snap, config)
    assert [(a.key, a.category) for a in new] == [("hw_motion_g_high", THRESHOLD)]

    engine2 = AlertEngine()
    r_err = parse_line('{"max30100":{"ok":true},"mpu":{"ok":false,"motion_g":0.9}}', received_at=1000.0)
    assert r_err.motion_g is None
    snap_err = build_snapshot({"status": "connected", "port": "COM5", "latest": r_err}, 5.0, now=1000.0)
    new_err, _ = engine2.evaluate(snap_err, config)
    assert [(a.key, a.category) for a in new_err] == [("hw_mpu_error", SENSOR_FAULT)]


def test_parse_missing_values_are_none():
    r = parse_line('{"t_ms":1,"max30100":{"ok":true,"hr_bpm":null,"spo2_pct":null}}')
    assert r.hr_bpm is None and r.spo2_pct is None


def test_parse_sensor_error_discards_numbers():
    r = parse_line('{"max30100":{"ok":false,"hr_bpm":80,"spo2_pct":95}}')
    assert r.sensor_ok is False and r.hr_bpm is None and r.spo2_pct is None


def test_parse_legacy_raw_format():
    r = parse_line("IR: 51234   RED: 40321")
    assert (r.ir_raw, r.red_raw, r.source_format, r.hr_bpm) == (51234, 40321, "legacy_raw", None)


@pytest.mark.parametrize("line", [
    '{"max30100":{"hr_bpm":72',                       # truncated JSON
    '{"max30100":{"hr_bpm":"seventy"}}',              # wrong type
    '{"max30100":{"hr_bpm":900}}',                    # outside sensor range
    '{"max30100":{"spo2_pct":140}}',
    '{"max30100":{"ok":"yes"}}',
    '{"t_ms":"abc"}',
    '[1,2,3]',
    "IR: 51234 RED:",                                 # partial legacy line
    "12345",
])
def test_parse_rejects_malformed(line):
    with pytest.raises(MalformedLine):
        parse_line(line)


def test_parse_ignores_status_messages():
    assert parse_line("MAX30100 initialized!") is None
    assert parse_line("") is None


# ---------------------------------------------------------------- serial reader
class FakeSerial:
    """Scripted serial port. Raises SerialException when the script hits 'DISCONNECT'."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.closed = False

    def readline(self):
        if not self.lines:
            time.sleep(0.05)
            return b""
        item = self.lines.pop(0)
        if item == "DISCONNECT":
            raise serial.SerialException("device unplugged")
        return item if isinstance(item, bytes) else (item + "\n").encode()

    def close(self):
        self.closed = True


def make_factory(scripts):
    """Each connection attempt gets the next script; None = port cannot be opened."""
    attempts = []

    def factory(port, baud):
        attempts.append(port)
        script = scripts.pop(0) if scripts else []
        if script is None:
            raise serial.SerialException("could not open port")
        return FakeSerial(script)
    return factory, attempts


def test_reader_valid_and_malformed_lines():
    factory, _ = make_factory([[
        '{"max30100":{"ok":true,"hr_bpm":70,"spo2_pct":98}}',
        "garbage {{{",
        b"\xff\xfe\xfa\n",  # invalid UTF-8 line
        '{"max30100":{"ok":true,"hr_bpm":71,"spo2_pct":98}}',
    ]])
    reader = SerialReader(serial_factory=factory, reconnect_seconds=0.05)
    reader.connect("COM_TEST1")
    try:
        assert wait_for(lambda: reader.snapshot()["valid_count"] == 2)
        snap = reader.snapshot()
        assert snap["status"] == "connected"
        assert snap["latest"].hr_bpm == 71
        assert snap["malformed_count"] == 2
    finally:
        reader.disconnect()


def test_reader_disconnect_and_reconnect():
    factory, attempts = make_factory([
        ['{"max30100":{"ok":true,"hr_bpm":70,"spo2_pct":98}}', "DISCONNECT"],
        None,                                                     # still unplugged
        ['{"max30100":{"ok":true,"hr_bpm":75,"spo2_pct":97}}'],   # plugged back in
    ])
    reader = SerialReader(serial_factory=factory, reconnect_seconds=0.05)
    reader.connect("COM_TEST2")
    try:
        assert wait_for(lambda: reader.snapshot()["latest"] is not None and reader.snapshot()["latest"].hr_bpm == 75)
        assert reader.snapshot()["status"] == "connected"
        assert len(attempts) == 3
        assert "unplugged" in reader.snapshot()["last_error"] or "open" in reader.snapshot()["last_error"]
    finally:
        reader.disconnect()
    assert reader.snapshot()["status"] == "disconnected"


def test_same_port_cannot_be_opened_twice():
    factory, _ = make_factory([[], []])
    a, b = SerialReader(serial_factory=factory), SerialReader(serial_factory=factory)
    assert a.connect("COM_SHARED") is True
    try:
        assert b.connect("COM_SHARED") is False
        assert b.snapshot()["status"] == "error"
    finally:
        a.disconnect()
        b.disconnect()


def test_mock_serial_is_labelled_simulated():
    reader = SerialReader()
    reader.connect(MOCK_PORT)
    try:
        assert wait_for(lambda: reader.snapshot()["latest"] is not None, timeout=3)
        latest = reader.snapshot()["latest"]
        assert latest.simulated is True
        assert latest.mlx_ok is True
        assert 20.0 <= latest.ambient_temp_c <= 40.0
        assert 25.0 <= latest.object_temp_c <= 45.0
    finally:
        reader.disconnect()


def test_continuous_serial_readings_and_chunked_buffering():
    """Simulate a continuous stream of 30 readings (including partial chunks and timeouts)
    and verify that:
      - all 30 readings are received in order without dropping or stalling,
      - calling connect() again with the same port/baud is a no-op that preserves state,
      - stale-data checks in build_snapshot never alter reader.status.
    """
    chunks = []
    for i in range(1, 31):
        line = (f'{{"t_ms":{i * 1000},"max30100":{{"ok":true,"hr_bpm":{60 + i},"spo2_pct":98}},'
                f'"mpu":{{"ok":true,"motion_g":0.02}}}}\n').encode()
        if i % 5 == 0:
            # Split line across two reads with an empty timeout read in between
            mid = len(line) // 2
            chunks.extend([line[:mid], b"", line[mid:]])
        else:
            chunks.append(line)

    class ChunkedFakeSerial:
        def __init__(self, items):
            self.items = list(items)
            self.closed = False

        @property
        def in_waiting(self):
            return 0

        def read(self, size=1):
            if not self.items:
                time.sleep(0.02)
                return b""
            return self.items.pop(0)

        def close(self):
            self.closed = True

    opens = []

    def factory(port, baud):
        opens.append((port, baud))
        return ChunkedFakeSerial(chunks)

    reader = SerialReader(serial_factory=factory, reconnect_seconds=0.05)
    assert reader.connect("COM_STREAM", 115200) is True
    try:
        assert wait_for(lambda: reader.snapshot()["valid_count"] == 30, timeout=5.0)
        snap = reader.snapshot()
        assert snap["status"] == "connected"
        assert snap["thread_alive"] is True
        assert snap["malformed_count"] == 0
        assert snap["latest"].hr_bpm == 90.0
        assert snap["latest"].device_ms == 30000

        # Redundant connect() on the same port/baud must NOT reopen the port or reset valid_count
        assert reader.connect("COM_STREAM", 115200) is True
        assert len(opens) == 1
        assert reader.snapshot()["valid_count"] == 30

        # Stale-data evaluation in build_snapshot must keep connection status separate from freshness
        stale_snap = build_snapshot(reader.snapshot(), stale_seconds=5.0, now=time.time() + 60.0)
        assert stale_snap.hw_status == "connected"
        assert stale_snap.hw_stale is True
        assert reader.snapshot()["status"] == "connected"
    finally:
        reader.disconnect()
    assert reader.snapshot()["status"] == "disconnected"
    assert reader.snapshot()["thread_alive"] is False


# ---------------------------------------------------------------- snapshot + presence
def hw_state(status="connected", hr=None, spo2=None, ok=True, age=0.0, now=1000.0):
    latest = None
    if status == "connected" or age:
        latest = parse_line(
            f'{{"max30100":{{"ok":{str(ok).lower()},"hr_bpm":{hr if hr is not None else "null"},'
            f'"spo2_pct":{spo2 if spo2 is not None else "null"}}}}}', received_at=now - age)
    return {"status": status, "port": "COM5", "latest": latest}


def test_stale_reading_is_not_used():
    snap = build_snapshot(hw_state(hr=80, age=10), stale_seconds=5, now=1000.0)
    assert snap.hw_stale and snap.hw_hr_bpm is None


def test_presence_tracker_absence_duration():
    t = PresenceTracker()
    assert t.update(True, 0.0) == ("present", 0.0)
    assert t.update(False, 0.1) == ("absent", pytest.approx(0.1))   # one missed frame
    assert t.update(True, 0.2) == ("present", 0.0)
    t.update(False, 1.0)
    assert t.update(False, 31.0) == ("absent", pytest.approx(30.8))
    assert t.update(None, 32.0) == ("unknown", 0.0)                   # camera unavailable


# ---------------------------------------------------------------- alert rules
def snap_at(now, **kw):
    hw = kw.pop("hw", hw_state(hr=kw.pop("hr", None), now=now))
    return build_snapshot(hw, 5.0, now=now, **kw)


def test_threshold_breach_requires_confirmation_and_alerts_once():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_hr_bpm": {"low": None, "high": 120}}, confirm_seconds=3, cooldown_seconds=300)
    raised = []
    for t in range(0, 20):
        new, _ = engine.evaluate(snap_at(1000.0 + t, hr=130), config)
        raised += new
    assert len(raised) == 1
    assert raised[0].category == THRESHOLD and raised[0].key == "hw_hr_bpm_high"
    assert raised[0].started_at == 1003.0  # after the 3 s confirmation


def test_flapping_breach_is_suppressed_by_cooldown():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_hr_bpm": {"low": None, "high": 120}}, confirm_seconds=0, cooldown_seconds=300)
    alerts = []
    for t, hr in enumerate([130, 100, 130, 100, 130]):
        new, _ = engine.evaluate(snap_at(1000.0 + t, hr=hr), config)
        alerts += new
    assert len(alerts) == 3
    assert [a.notify for a in alerts] == [True, False, False]
    assert alerts[1].delivery_status == "suppressed (cooldown)"


def test_missing_reading_is_not_a_threshold_breach():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_hr_bpm": {"low": 50, "high": 120}}, confirm_seconds=0)
    new, _ = engine.evaluate(snap_at(1000.0, hr=None), config)
    assert new == []


def test_disconnected_is_sensor_fault_not_threshold():
    engine = AlertEngine()
    config = cfg(thresholds={"hw_hr_bpm": {"low": 50, "high": 120}}, hardware_fault_alerts=True, confirm_seconds=0)
    new, _ = engine.evaluate(snap_at(1000.0, hw=hw_state(status="reconnecting")), config)
    assert [(a.key, a.category) for a in new] == [("hw_disconnected", SENSOR_FAULT)]
    # user deliberately disconnected -> no fault alert
    engine2 = AlertEngine()
    new, _ = engine2.evaluate(snap_at(1000.0, hw=hw_state(status="disconnected")), config, hardware_expected=False)
    assert new == []


def test_absence_alert_after_configured_duration_only():
    engine = AlertEngine()
    config = cfg(presence={"absence_seconds": 30})
    new, _ = engine.evaluate(snap_at(1000.0, presence="absent", absent_seconds=10), config)
    assert new == []
    new, _ = engine.evaluate(snap_at(1001.0, presence="absent", absent_seconds=31), config)
    assert [(a.key, a.category) for a in new] == [("patient_absent", PRESENCE)]
    _, cleared = engine.evaluate(snap_at(1002.0, presence="present"), config)
    assert [a.key for a in cleared] == ["patient_absent"]


def test_camera_unavailable_never_counts_as_absent():
    engine = AlertEngine()
    config = cfg(presence={"absence_seconds": 1})
    new, _ = engine.evaluate(snap_at(1000.0, presence="unknown", camera_status="unavailable"), config)
    assert new == []


def test_disagreement_needs_usable_camera_quality():
    engine = AlertEngine()
    config = cfg(disagreement_bpm=15, confirm_seconds=0)
    new, _ = engine.evaluate(snap_at(1000.0, hr=70, cam_bpm=100, cam_quality="Poor"), config)
    assert new == []
    new, _ = engine.evaluate(snap_at(1001.0, hr=70, cam_bpm=100, cam_quality="Good"), config)
    assert [a.key for a in new] == ["hr_disagreement"]


def test_config_validation():
    assert validate_config(cfg()) == []
    errors = validate_config(cfg(thresholds={"hw_hr_bpm": {"low": 130, "high": 120},
                                             "cam_bpm": {"low": None, "high": 300}},
                                 email={"enabled": True, "recipient": ""}))
    assert len(errors) == 3


# ---------------------------------------------------------------- email
def make_alert(simulated=False):
    return Alert(key="hw_hr_bpm_high", category=THRESHOLD, message="HR above limit",
                 started_at=1000.0, readings={"Sensor heart rate (MAX30100)": "130 BPM"}, simulated=simulated)


@pytest.fixture
def smtp_env(monkeypatch, tmp_path):
    monkeypatch.setattr("notifier.ENV_FILE", str(tmp_path / "missing.env"))
    for key in ("PV_SMTP_HOST", "PV_SMTP_USER", "PV_SMTP_PASSWORD", "PV_SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PV_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("PV_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("PV_SMTP_PASSWORD", "secret-password")


def test_email_failure_does_not_crash(smtp_env):
    def failing_sender(msg, settings):
        raise ConnectionRefusedError("server down")

    done = threading.Event()
    results = []
    notifier = EmailNotifier(sender_func=failing_sender)
    notifier.notify(make_alert(), "carer@example.com", on_done=lambda a, s: (results.append(s), done.set()))
    assert done.wait(3)
    assert results[0].startswith("failed:") and "secret-password" not in results[0]


def test_email_success_reports_sent(smtp_env):
    sent = []
    done = threading.Event()
    notifier = EmailNotifier(sender_func=lambda msg, settings: sent.append(msg))
    notifier.notify(make_alert(simulated=True), "carer@example.com", on_done=lambda a, s: done.set())
    assert done.wait(3)
    assert notifier.last_result == ("hw_hr_bpm_high", "sent")
    assert sent[0]["Subject"].startswith("[SIMULATED]")
    assert "130 BPM" in sent[0].get_content()


def test_email_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("notifier.ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("PV_SMTP_HOST", raising=False)
    status = EmailNotifier().notify(make_alert(), "carer@example.com")
    assert status.startswith("failed: email not configured")


def test_email_message_contents():
    msg = build_message(make_alert(), "carer@example.com", "sender@example.com")
    body = msg.get_content()
    assert "THRESHOLD" in body and "HR above limit" in body and "NOT a medical device" in body


# ---------------------------------------------------------------- storage
def test_recorder_appends_rows_and_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    recorder = DataRecorder()
    recorder.FLUSH_SECONDS = 0.1
    for t in range(3):
        recorder.log_snapshot(snap_at(1000.0 + t, hr=70 + t, session_id="S1"))
    alert = make_alert()
    recorder.record_alert(alert)
    recorder.update_alert(alert.uid, delivery_status="sent", cleared_at="2026-01-01T00:00:00")
    recorder.log_snapshot(snap_at(1004.0, hr=None, session_id="S1"))  # missing value
    recorder.flush(timeout=10)

    log_df = database.load_monitor_log("S1")
    assert list(log_df["hw_hr_bpm"].iloc[:3]) == [70, 71, 72]
    assert log_df["hw_hr_bpm"].isna().iloc[3]
    alerts_df = database.load_alerts()
    assert alerts_df.iloc[0]["delivery_status"] == "sent"
    # a second recorder appends, it does not overwrite
    recorder2 = DataRecorder()
    recorder2.log_snapshot(snap_at(1005.0, hr=80, session_id="S1"))
    recorder2.flush(timeout=10)
    assert len(database.load_monitor_log("S1")) == 5
    assert recorder.last_error is None and recorder2.last_error is None


def test_storage_error_does_not_raise(monkeypatch):
    def broken(rows):
        raise OSError("disk full")
    monkeypatch.setattr(database, "insert_monitor_rows", broken)
    recorder = DataRecorder()
    recorder.log_snapshot(snap_at(1000.0, hr=70))
    recorder.flush(timeout=10)
    assert "disk full" in recorder.last_error


# ---------------------------------------------------------------- existing CV pipeline unchanged
def test_rppg_pipeline_recovers_synthetic_pulse():
    from rppg import pos
    from signal_processing import SAMPLE_RATE, bandpass_filter, find_heart_rate, power_spectrum

    t = np.arange(0, 12, 1 / SAMPLE_RATE)
    pulse = 0.01 * np.sin(2 * np.pi * 1.2 * t)  # 72 BPM, ~1 % skin colour change
    rgb = np.column_stack([150 * (1 + 0.3 * pulse), 100 * (1 + pulse), 80 * (1 + 0.5 * pulse)])
    rgb += np.random.default_rng(0).normal(0, 0.05, rgb.shape)
    bpm, ratio = find_heart_rate(*power_spectrum(bandpass_filter(pos(rgb, SAMPLE_RATE))))
    assert abs(bpm - 72) <= 2 and ratio > 0.6
