# Firebase notifications setup

PulseVision writes alert and notification records to SQLite immediately, then
publishes real alert events to Firestore from a server-side background thread.
Cloud Functions send FCM push notifications and optional MSG91 SMS. Firestore
stores the durable alert, notification, and lifecycle event history. The Streamlit
feed refreshes from its local mirror every two seconds; Firestore synchronization
runs off the sensor and camera processing thread.

No Firebase project, service account, FCM token, phone number, or provider secret
is included in this repository. Do not add those values to Git.

## 1. Create and select a Firebase project

Install Node.js 20 and the Firebase CLI, then from `Pulse-Vision`:

```powershell
firebase login
firebase use --add
```

Select an existing Firebase project or create one in the Firebase console. Enable
Cloud Firestore and Firebase Cloud Messaging for that project.

## 2. Install and deploy the notification functions

```powershell
cd firebase\functions
npm install
npm run build
cd ..\..
firebase deploy --only functions,firestore:rules
```

The Firestore rules deny direct browser access. PulseVision and Cloud Functions
use trusted server-side Admin SDK credentials.

## 3. Configure provider secrets

For SMS, create an MSG91 Flow/template and sender ID. Store the MSG91 auth key
and a separate random delivery-receipt secret in Firebase Secret Manager:

```powershell
firebase functions:secrets:set MSG91_AUTH_KEY
firebase functions:secrets:set MSG91_DLR_SECRET
firebase deploy --only functions
```

Configure the MSG91 delivery-receipt callback to call the deployed
`msg91DeliveryReceipt` function with its `key` query parameter set to the same
`MSG91_DLR_SECRET`. The function only changes the status to `DELIVERED` after a
valid provider receipt. A successful provider API response is recorded as
`SENT` (accepted), never as delivered.

For push, a client device must register an FCM token. This repository does not
include a mobile client or token-registration flow, so push cannot send until a
valid token is entered in the server-side settings form.

## 4. Configure PulseVision server credentials

Use Google Application Default Credentials or a service account JSON stored
outside the repository. Set the Firebase project ID in the server environment:

```powershell
$env:PV_FIREBASE_PROJECT_ID = "your-firebase-project-id"
# Optional when not using Application Default Credentials:
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\secure\path\pulsevision-service-account.json"
```

Install Python dependencies from the project root if needed:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Start the dashboard:

```powershell
.\.venv\Scripts\streamlit run app.py
```

In **Notifications & Alerts → Patient notification settings**, enter a patient
identifier and choose `push`, `sms`, or `both`. Phone numbers and FCM tokens are
stored in the server-only Firestore configuration document. The dashboard masks
phone numbers. For SMS, provide the MSG91 flow/template and sender IDs. Enable
notifications only after Firebase and provider configuration are ready.

## Lifecycle and retry behavior

- Real alert events are assigned the alert engine's unique event ID and use it as
  the Firestore notification document ID. Create-only writes prevent duplicate
  notification documents for the same event.
- Cloud Functions record `DETECTED`, `QUEUED`, `SENDING`, and then `SENT` or
  `FAILED`. Retries reuse the same notification ID and require a transactionally
  verified `FAILED` state.
- FCM Admin acceptance does not provide a server-side delivery confirmation, so
  push notifications remain `SENT`. MSG91 can become `DELIVERED` only through
  its authenticated delivery-receipt callback.
- Alert recovery updates `conditionStatus=RECOVERED` independently of notification
  delivery status. A failed notification does not keep the alert condition active.
- The explicit temperature test button creates a local event marked `TEST`; it
  never writes a provider queue record or sends SMS/push.
- If Firebase is unavailable, the local SQLite audit remains available and the
  notification row records a safe, non-secret failure reason.

Do not use the displayed prototype measurements for clinical decisions.
