"""Focused notification audit tests; no Firebase or SMS provider is contacted."""

import time

import database
from alerts import Alert
from firebase_notifications import FirebaseNotificationService, build_notification_record, mask_phone
from monitor import Snapshot
from settings import DEFAULT_CONFIG


def test_phone_is_masked():
    assert mask_phone("+91 98765 43210") == "••••••3210"
    assert mask_phone("") == "Not configured"


def test_test_temperature_notification_is_local_and_marked_test(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "notifications.db"))
    now = time.time()
    alert = Alert(
        key="hw_object_temp_c_high", category="TEST",
        message="TEST ONLY: simulated skin surface temperature breach.",
        started_at=now, readings={}, simulated=True, notify=False,
    )
    snap = Snapshot(time=now, timestamp="2026-11-11T12:00:00")
    config = dict(DEFAULT_CONFIG)
    config["notifications"] = {"enabled": True, "patient_id": "P-1", "channel": "sms", "phone": "+919876543210"}

    row = build_notification_record(alert, config, snap)
    assert row["delivery_status"] == "DETECTED"
    assert row["measured_value"] == "TEST sample: 38.0 °C (simulated only)"
    assert row["recipient_masked"] == "••••••3210"
    assert row["simulated"] == 1

    service = FirebaseNotificationService()
    result = service.record_alert(alert, config, snap)
    assert result["delivery_status"] == "TEST"
    stored = database.load_notifications()
    assert len(stored) == 1
    assert stored.iloc[0]["delivery_status"] == "TEST"
    assert "TEST" in stored.iloc[0]["trigger_reason"]
    history = database.load_notification_events(alert.uid)
    assert history["status"].tolist() == ["DETECTED", "TEST"]


def test_recovery_is_separate_from_notification_result(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "notifications.db"))
    now = time.time()
    alert = Alert(
        key="hw_object_temp_c_high", category="TEST",
        message="TEST only", started_at=now, readings={}, simulated=True, notify=False,
    )
    snap = Snapshot(time=now, timestamp="2026-11-11T12:00:00")
    service = FirebaseNotificationService()
    service.record_alert(alert, DEFAULT_CONFIG, snap)
    service.mark_recovered(alert)

    stored = database.load_notifications().iloc[0]
    assert stored["condition_status"] == "RECOVERED"
    assert stored["delivery_status"] == "TEST"
    assert database.load_notification_events(alert.uid)["status"].tolist() == [
        "DETECTED", "TEST", "CONDITION_RECOVERED"]
