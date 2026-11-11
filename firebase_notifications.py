"""Server-side Firebase bridge for alert history and notification delivery.

Firebase Admin credentials are loaded only by the Python server (ADC or
GOOGLE_APPLICATION_CREDENTIALS). SMS/FCM credentials belong in Cloud Functions
secrets; this module never sends directly to a provider.
"""

import logging
import os
import threading
from datetime import datetime, timezone

import database
from settings import METRICS

log = logging.getLogger("pulsevision.firebase")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_firebase_error(exc):
    if isinstance(exc, ImportError):
        return "firebase-admin is not installed; install requirements.txt"
    if not os.environ.get("PV_FIREBASE_PROJECT_ID"):
        return "PV_FIREBASE_PROJECT_ID is not configured"
    if "credential" in type(exc).__name__.lower() or "credential" in str(exc).lower():
        return "Firebase server credentials are unavailable or invalid"
    return f"Firestore request failed ({type(exc).__name__}); see server logs"


def mask_phone(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return "Not configured"
    return "••••••" + digits[-4:]


def _metric_for(alert_key):
    for metric in METRICS:
        if alert_key.startswith(metric + "_"):
            return metric
    return None


def build_notification_record(alert, config, snap):
    """Create an auditable notification record without retaining a raw phone number."""
    metric = _metric_for(alert.key)
    notification = config.get("notifications", {})
    if metric:
        meta = METRICS[metric]
        value = getattr(snap, metric)
        direction = "high" if alert.key.endswith("_high") else "low"
        threshold = config.get("thresholds", {}).get(metric, {}).get(direction)
        parameter = meta["label"]
        measured = f"{value:g} {meta['unit']}" if value is not None else "unavailable"
        if alert.simulated and alert.category == "TEST" and metric == "hw_object_temp_c":
            measured = "TEST sample: 38.0 °C (simulated only)"
        configured = f"{threshold:g} {meta['unit']}" if threshold is not None else "not configured"
    elif alert.key == "patient_absent":
        parameter = "Patient presence"
        measured = f"Not detected for {snap.absent_seconds:.1f} s"
        threshold = config.get("presence", {}).get("absence_seconds")
        configured = f"{threshold:g} s" if threshold is not None else "not configured"
    else:
        parameter = alert.key.replace("_", " ").title()
        measured = alert.message
        configured = "Condition based"

    now = _now()
    status = "DETECTED"
    row = {
        "notification_id": alert.uid,
        "alert_event_id": alert.uid,
        "alert_key": alert.key,
        "category": alert.category,
        "patient_id": notification.get("patient_id") or "Unassigned",
        "parameter": parameter,
        "trigger_reason": alert.message,
        "measured_value": measured,
        "configured_threshold": configured,
        "event_timestamp": alert.timestamp,
        "recipient_masked": mask_phone(notification.get("phone")),
        "channel": (notification.get("channel") or "push").upper(),
        "delivery_status": status,
        "condition_status": "ACTIVE",
        "provider": "Firebase Cloud Functions",
        "provider_message_id": None,
        "attempt_count": 0,
        "last_attempt_at": None,
        "delivered_at": None,
        "failure_reason": None,
        "simulated": int(alert.simulated),
        "updated_at": now,
    }
    return row


class FirebaseNotificationService:
    """Persists every alert locally and publishes real alerts to Firestore off-thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._init_error = None
        self._sync_thread = None
        self._sync_stop = threading.Event()
        self._sync_cursor = None
        self._last_sync_error = None

    def _firestore(self):
        from notifier import load_env_file
        load_env_file()
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                import firebase_admin
                from firebase_admin import credentials, firestore

                project_id = os.environ.get("PV_FIREBASE_PROJECT_ID")
                if not project_id:
                    raise RuntimeError("PV_FIREBASE_PROJECT_ID is not configured")
                try:
                    app = firebase_admin.get_app()
                except ValueError:
                    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                    credential = (credentials.Certificate(key_path) if key_path
                                  else credentials.ApplicationDefault())
                    app = firebase_admin.initialize_app(credential, {"projectId": project_id})
                self._client = firestore.client(app=app)
                self._init_error = None
            except Exception as exc:  # noqa: BLE001 - missing cloud config must not stop monitoring
                self._init_error = _safe_firebase_error(exc)
                raise RuntimeError(self._init_error) from exc
        return self._client

    def save_patient_config(self, notification_config):
        """Save private recipient routing under a server-only Firestore document."""
        patient_id = (notification_config.get("patient_id") or "").strip()
        if not patient_id or "/" in patient_id:
            raise ValueError("Enter a patient identifier without '/'.")
        data = {
            "patientId": patient_id,
            "phone": (notification_config.get("phone") or "").strip(),
            "channel": (notification_config.get("channel") or "push").lower(),
            "enabled": bool(notification_config.get("enabled", True)),
            "fcmToken": (notification_config.get("fcm_token") or "").strip(),
            "msg91FlowId": (notification_config.get("msg91_flow_id") or "").strip(),
            "msg91SenderId": (notification_config.get("msg91_sender_id") or "").strip(),
            "updatedAt": _now(),
        }
        self._firestore().collection("patients").document(patient_id).collection(
            "notification_config").document("settings").set(data)
        return mask_phone(data["phone"])

    def record_alert(self, alert, config, snap):
        """Write DETECTED locally immediately and queue Firebase publication in a daemon."""
        row = build_notification_record(alert, config, snap)
        database.upsert_notification(row, event_detail=alert.message)
        if alert.simulated:
            test_row = dict(row, delivery_status="TEST", updated_at=_now())
            database.upsert_notification(test_row, event_detail="TEST event; no provider send performed")
            return test_row
        dispatch_enabled = bool(config.get("notifications", {}).get("enabled", False) and alert.notify)
        if dispatch_enabled:
            row = dict(row, delivery_status="QUEUED", updated_at=_now())
            database.upsert_notification(row, event_detail="Queued for Firebase notification processing")
        elif not alert.notify:
            row = dict(row, delivery_status="SUPPRESSED", updated_at=_now())
            database.upsert_notification(row, event_detail="Suppressed by alert cooldown")
        threading.Thread(target=self._publish_worker, args=(alert, row, dispatch_enabled),
                         name=f"firebase-alert-{alert.uid[:8]}", daemon=True).start()
        return row

    def _publish_worker(self, alert, row, dispatch_enabled):
        try:
            client = self._firestore()
            alert_doc = {
                "alertEventId": alert.uid,
                "patientId": row["patient_id"],
                "alertKey": row["alert_key"],
                "category": row["category"],
                "reason": alert.message,
                "conditionStatus": "ACTIVE",
                "simulated": False,
                "dispatchEnabled": dispatch_enabled,
                "cooldownSuppressed": not alert.notify,
                "createdAt": row["event_timestamp"],
            }
            notification_doc = {
                "notificationId": row["notification_id"],
                "alertEventId": alert.uid,
                "patientId": row["patient_id"],
                "alertKey": row["alert_key"],
                "category": row["category"],
                "parameter": row["parameter"],
                "measuredValue": row["measured_value"],
                "configuredThreshold": row["configured_threshold"],
                "reason": alert.message,
                "eventTimestamp": row["event_timestamp"],
                "channel": row["channel"].lower(),
                "status": "DETECTED",
                "conditionStatus": "ACTIVE",
                "attemptCount": 0,
                "simulated": False,
                "dispatchEnabled": dispatch_enabled,
                "cooldownSuppressed": not alert.notify,
                "retryRequested": 0,
                "createdAt": row["updated_at"],
                "updatedAt": row["updated_at"],
            }
            # Create-only writes make alert IDs idempotent. A duplicate trigger cannot
            # create a second notification with the same unique alert event ID.
            notification_ref = client.collection("notifications").document(alert.uid)
            alert_ref = client.collection("alerts").document(alert.uid)
            batch = client.batch()
            batch.create(alert_ref, alert_doc)
            batch.create(notification_ref, notification_doc)
            event_ref = notification_ref.collection("events").document()
            batch.create(event_ref, {
                "status": "DETECTED", "detail": alert.message,
                "timestamp": row["event_timestamp"],
            })
            batch.commit()
            patient_settings = client.collection("patients").document(row["patient_id"]).collection(
                "notification_config").document("settings").get()
            phone = (patient_settings.to_dict() or {}).get("phone") if patient_settings.exists else ""
            if phone:
                notification_ref.update({"recipientMasked": mask_phone(phone)})
        except Exception as exc:  # noqa: BLE001 - provider/cloud failures stay out of acquisition
            reason = f"Firebase queue failed: {_safe_firebase_error(exc)}"[:400]
            failed = dict(row, delivery_status="FAILED", failure_reason=reason, updated_at=_now())
            try:
                database.upsert_notification(failed, event_detail=reason)
            except Exception:
                log.exception("Could not persist Firebase failure for alert %s", alert.uid)
            log.error("Firebase notification enqueue failed for %s: %s", alert.uid, reason)

    def mark_recovered(self, alert):
        """Record recovery separately from provider delivery state."""
        try:
            row_df = database.load_notifications(limit=1000)
            match = row_df[row_df["notification_id"] == alert.uid]
            if match.empty:
                return
            row = match.iloc[0].to_dict()
            row["condition_status"] = "RECOVERED"
            row["updated_at"] = _now()
            database.upsert_notification(
                row, event_detail="Alert condition recovered", event_status="CONDITION_RECOVERED")
        except Exception:
            log.exception("Could not record recovered condition for %s", alert.uid)
        try:
            client = self._firestore()
            client.collection("alerts").document(alert.uid).update(
                {"conditionStatus": "RECOVERED", "recoveredAt": _now()})
            client.collection("notifications").document(alert.uid).update(
                {"conditionStatus": "RECOVERED", "updatedAt": _now()})
            client.collection("notifications").document(alert.uid).collection("events").add({
                "status": "CONDITION_RECOVERED", "detail": "Alert condition recovered", "timestamp": _now()})
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not publish recovery for %s: %s", alert.uid, exc)

    def retry_notification(self, notification_id):
        """Request one idempotent retry for a failed notification."""
        from firebase_admin import firestore

        client = self._firestore()
        ref = client.collection("notifications").document(notification_id)

        @firestore.transactional
        def request(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                raise ValueError("Notification is not present in Firebase.")
            data = snapshot.to_dict() or {}
            if data.get("status") != "FAILED":
                raise ValueError("Only failed notifications can be retried.")
            next_request = int(data.get("retryRequested", 0)) + 1
            transaction.update(ref, {
                "status": "RETRYING", "retryRequested": next_request,
                "updatedAt": _now(), "failureReason": None,
            })
            return next_request

        txn = client.transaction()
        attempt = request(txn)
        ref.collection("events").add({"status": "RETRYING", "detail": f"Manual retry {attempt}", "timestamp": _now()})
        return attempt

    def refresh_from_firebase(self):
        """Pull authoritative Cloud Function status changes into local audit records."""
        try:
            client = self._firestore()
            collection = client.collection("notifications")
            if self._sync_cursor is None:
                docs = list(collection.order_by("updatedAt", direction="DESCENDING").limit(500).stream())
                docs.reverse()
            else:
                docs = collection.where("updatedAt", ">", self._sync_cursor).order_by("updatedAt").limit(500).stream()
            synced = 0
            for doc in docs:
                data = doc.to_dict() or {}
                notification_id = data.get("notificationId", doc.id)
                local = {
                    "notification_id": notification_id,
                    "alert_event_id": data.get("alertEventId", doc.id),
                    "alert_key": data.get("alertKey", "unknown"),
                    "category": data.get("category", "UNKNOWN"),
                    "patient_id": data.get("patientId", "Unassigned"),
                    "parameter": data.get("parameter"),
                    "trigger_reason": data.get("reason"),
                    "measured_value": data.get("measuredValue"),
                    "configured_threshold": data.get("configuredThreshold"),
                    "event_timestamp": data.get("eventTimestamp", data.get("createdAt", _now())),
                    "recipient_masked": data.get("recipientMasked", "Configured in Firebase"),
                    "channel": str(data.get("channel", "push")).upper(),
                    "delivery_status": str(data.get("status", "QUEUED")).upper(),
                    "condition_status": str(data.get("conditionStatus", "ACTIVE")).upper(),
                    "provider": data.get("provider", "Firebase Cloud Functions"),
                    "provider_message_id": data.get("providerMessageId"),
                    "attempt_count": int(data.get("attemptCount", 0)),
                    "last_attempt_at": data.get("lastAttemptAt"),
                    "delivered_at": data.get("deliveredAt"),
                    "failure_reason": data.get("failureReason"),
                    "simulated": int(bool(data.get("simulated", False))),
                    "updated_at": data.get("updatedAt", _now()),
                }
                database.upsert_notification(local, append_event=False)
                for event_doc in doc.reference.collection("events").stream():
                    event_data = event_doc.to_dict() or {}
                    event_time = event_data.get("timestamp", _now())
                    if hasattr(event_time, "isoformat"):
                        event_time = event_time.isoformat()
                    database.insert_notification_event(
                        notification_id,
                        str(event_data.get("status", "UNKNOWN")).upper(),
                        str(event_time), event_data.get("detail"), f"firebase:{event_doc.id}",
                    )
                synced += 1
                updated_at = str(local["updated_at"])
                if self._sync_cursor is None or updated_at > self._sync_cursor:
                    self._sync_cursor = updated_at
            self._last_sync_error = None
            return synced
        except Exception as exc:  # noqa: BLE001 - local feed remains available offline
            self._init_error = _safe_firebase_error(exc)
            self._last_sync_error = self._init_error
            return 0

    def start_background_sync(self, interval_seconds=2.0):
        """Poll Firestore off the monitoring/UI thread; the UI reads the local mirror."""
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return
        self._sync_stop.clear()

        def run():
            while not self._sync_stop.is_set():
                self.refresh_from_firebase()
                self._sync_stop.wait(interval_seconds)

        self._sync_thread = threading.Thread(target=run, name="firebase-notification-sync", daemon=True)
        self._sync_thread.start()

    @property
    def status(self):
        if self._client is None:
            return self._init_error or "not configured"
        return "connected" if self._last_sync_error is None else f"unavailable: {self._last_sync_error}"
