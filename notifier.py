"""Email alerts over SMTP (Python standard library only).

Credentials come from environment variables (or a local .env file, which is
git-ignored) and are never logged:

  PV_SMTP_HOST       e.g. smtp.gmail.com
  PV_SMTP_PORT       e.g. 587 (STARTTLS) or 465 (SSL)
  PV_SMTP_USER       login name
  PV_SMTP_PASSWORD   password or app password
  PV_SMTP_FROM       sender address (defaults to PV_SMTP_USER)
  PV_SMTP_SECURITY   "starttls" (default), "ssl" or "none"

The recipient is set in the dashboard (config.json). Sending happens in a
background thread so a slow or failing mail server never blocks monitoring.
"""

import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

log = logging.getLogger("pulsevision.notifier")

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
SMTP_TIMEOUT_SECONDS = 15


def load_env_file(path=ENV_FILE):
    """Load KEY=VALUE lines from .env without overriding variables already set."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def smtp_settings():
    """Return the SMTP settings from the environment (password included - do not log)."""
    load_env_file()
    user = os.environ.get("PV_SMTP_USER", "")
    return {
        "host": os.environ.get("PV_SMTP_HOST", ""),
        "port": int(os.environ.get("PV_SMTP_PORT", "587") or 587),
        "user": user,
        "password": os.environ.get("PV_SMTP_PASSWORD", ""),
        "sender": os.environ.get("PV_SMTP_FROM", "") or user,
        "security": os.environ.get("PV_SMTP_SECURITY", "starttls").lower(),
    }


def missing_settings(settings=None):
    """Names of required environment variables that are not set."""
    settings = settings or smtp_settings()
    required = {"PV_SMTP_HOST": settings["host"], "PV_SMTP_FROM / PV_SMTP_USER": settings["sender"]}
    if settings["user"]:
        required["PV_SMTP_PASSWORD"] = settings["password"]
    return [name for name, value in required.items() if not value]


def build_message(alert, recipient, sender):
    msg = EmailMessage()
    prefix = "[SIMULATED] " if alert.simulated else ""
    msg["Subject"] = f"{prefix}PulseVision alert: {alert.category} - {alert.key}"
    msg["From"] = sender
    msg["To"] = recipient
    lines = [
        "PulseVision has detected an alert condition.",
        "",
        f"Alert type: {alert.category} ({alert.key})",
        f"Time:       {alert.timestamp}",
        f"Details:    {alert.message}",
        "",
        "Readings at the time of the alert:",
    ]
    lines += [f"  - {name}: {value}" for name, value in alert.readings.items()]
    if alert.simulated:
        lines += ["", "NOTE: hardware readings are SIMULATED test data, not real patient measurements."]
    lines += ["", "PulseVision is a student prototype and NOT a medical device. "
              "Please check on the patient directly."]
    msg.set_content("\n".join(lines))
    return msg


def send_email(msg, settings):
    """Send one message. Raises on failure."""
    context = ssl.create_default_context()
    if settings["security"] == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS)
    with server:
        if settings["security"] == "starttls":
            server.starttls(context=context)
        if settings["user"]:
            server.login(settings["user"], settings["password"])
        server.send_message(msg)


class EmailNotifier:
    """Sends alert emails in background threads and reports the delivery result."""

    def __init__(self, sender_func=send_email):
        self._send = sender_func
        self.last_result = None

    def notify(self, alert, recipient, on_done=None):
        """Send the alert email without blocking. on_done(alert, status) is called afterwards."""
        settings = smtp_settings()
        missing = missing_settings(settings)
        if not recipient:
            return self._finish(alert, "failed: no recipient configured", on_done)
        if missing:
            return self._finish(alert, "failed: email not configured (" + ", ".join(missing) + ")", on_done)
        threading.Thread(target=self._worker, args=(alert, recipient, settings, on_done),
                         name="email-alert", daemon=True).start()
        return None

    def _worker(self, alert, recipient, settings, on_done):
        try:
            self._send(build_message(alert, recipient, settings["sender"]), settings)
            status = "sent"
            log.info("Alert email sent (%s)", alert.key)
        except Exception as exc:  # noqa: BLE001 - delivery problems must never crash monitoring
            # The exception text from smtplib never contains the password
            status = f"failed: {type(exc).__name__}: {exc}"[:300]
            log.error("Alert email failed (%s): %s", alert.key, status)
        self._finish(alert, status, on_done)

    def _finish(self, alert, status, on_done):
        alert.delivery_status = status
        self.last_result = (alert.key, status)
        if on_done is not None:
            try:
                on_done(alert, status)
            except Exception:  # noqa: BLE001
                log.exception("Delivery callback failed")
        return status
