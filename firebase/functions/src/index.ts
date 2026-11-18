import { initializeApp } from "firebase-admin/app";
import { getFirestore, FieldValue } from "firebase-admin/firestore";
import { getMessaging } from "firebase-admin/messaging";
import { defineSecret } from "firebase-functions/params";
import { onDocumentCreated, onDocumentUpdated } from "firebase-functions/v2/firestore";
import { onRequest } from "firebase-functions/v2/https";
import { logger } from "firebase-functions";
import { timingSafeEqual } from "node:crypto";

initializeApp();
const db = getFirestore();
const MSG91_AUTH_KEY = defineSecret("MSG91_AUTH_KEY");
const MSG91_DLR_SECRET = defineSecret("MSG91_DLR_SECRET");

type Notice = {
  notificationId: string;
  alertEventId: string;
  patientId: string;
  category: string;
  alertKey: string;
  parameter: string;
  measuredValue: string;
  configuredThreshold: string;
  reason: string;
  channel: "push" | "sms" | "both";
  status: string;
  attemptCount: number;
  simulated?: boolean;
};

function appendStatus(id: string, status: string, detail?: string) {
  return db.collection("notifications").doc(id).collection("events").add({
    status, detail: detail || null, timestamp: FieldValue.serverTimestamp(),
  });
}

function maskPhone(value: unknown): string {
  const digits = String(value || "").replace(/\D/g, "");
  return digits ? `••••••${digits.slice(-4)}` : "Not configured";
}

async function transition(id: string, status: string, fields: Record<string, unknown> = {}) {
  await db.collection("notifications").doc(id).update({
    status, ...fields, updatedAt: new Date().toISOString(),
  });
  await appendStatus(id, status, fields.failureReason as string | undefined);
}

async function queueNewNotification(id: string): Promise<boolean> {
  const ref = db.collection("notifications").doc(id);
  const queued = await db.runTransaction(async (tx) => {
    const snap = await tx.get(ref);
    if (!snap.exists || snap.data()?.status !== "DETECTED") return false;
    tx.update(ref, { status: "QUEUED", updatedAt: new Date().toISOString() });
    return true;
  });
  if (queued) await appendStatus(id, "QUEUED");
  return queued;
}

async function deliver(id: string, retry: boolean) {
  const ref = db.collection("notifications").doc(id);
  const claim = await db.runTransaction(async (tx) => {
    const snap = await tx.get(ref);
    if (!snap.exists) return null;
    const data = snap.data() as Notice;
    const allowed = retry ? data.status === "RETRYING" : data.status === "QUEUED";
    if (!allowed || data.simulated) return null;
    const attemptCount = Number(data.attemptCount || 0) + 1;
    tx.update(ref, {
      status: "SENDING", attemptCount, lastAttemptAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
    return { ...data, attemptCount };
  });
  if (!claim) return;
  await appendStatus(id, "SENDING", `Attempt ${claim.attemptCount}`);
  try {
    const configSnap = await db.collection("patients").doc(claim.patientId)
      .collection("notification_config").doc("settings").get();
    if (!configSnap.exists) throw new Error("No Firebase notification settings for this patient");
    const config = configSnap.data()!;
    if (!config.enabled) throw new Error("Notifications are disabled for this patient");
    const channels = claim.channel === "both" ? ["push", "sms"] : [claim.channel];
    const accepted: string[] = [];
    const references: string[] = [];
    let smsReference: string | null = null;
    for (const channel of channels) {
      if (channel === "push") {
        if (!config.fcmToken) throw new Error("No FCM device token configured");
        const messageId = await getMessaging().send({
          token: String(config.fcmToken),
          notification: {
            title: `PulseVision ${claim.category} alert`,
            body: `${claim.parameter}: ${claim.measuredValue}; threshold ${claim.configuredThreshold}`,
          },
          data: { notificationId: id, alertEventId: claim.alertEventId },
        });
        accepted.push("push");
        references.push(messageId);
      } else if (channel === "sms") {
        if (!config.phone || !config.msg91FlowId || !config.msg91SenderId) {
          throw new Error("SMS requires phone, MSG91 flow ID, and sender ID in patient configuration");
        }
        const response = await fetch("https://control.msg91.com/api/v5/flow/", {
          method: "POST",
          headers: { "Content-Type": "application/json", authkey: MSG91_AUTH_KEY.value() },
          body: JSON.stringify({
            template_id: config.msg91FlowId,
            sender: config.msg91SenderId,
            mobiles: config.phone,
            VAR1: claim.parameter,
            VAR2: claim.measuredValue,
            VAR3: claim.configuredThreshold,
            VAR4: claim.patientId,
          }),
        });
        const result = await response.json() as { request_id?: string; message?: string };
        if (!response.ok) throw new Error(`MSG91 rejected request (${response.status}): ${result.message || "provider error"}`);
        accepted.push("sms");
        if (result.request_id) {
          references.push(result.request_id);
          smsReference = result.request_id;
        }
      }
    }
    // SENT means provider accepted. Delivery receipts (when supported) change it
    // to DELIVERED separately; HTTP 200 is never treated as delivery confirmation.
    await transition(id, "SENT", {
      provider: accepted.join(" + "), providerMessageId: references.join(",") || null,
      msg91RequestId: smsReference,
      recipientMasked: maskPhone(config.phone), failureReason: null,
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : "Unknown provider failure";
    await transition(id, "FAILED", { failureReason: reason });
    logger.error("PulseVision notification failed", { id, reason });
  }
}

export const dispatchNewNotification = onDocumentCreated(
  { document: "notifications/{notificationId}", secrets: [MSG91_AUTH_KEY] }, async (event) => {
    const id = event.params.notificationId;
    const snap = event.data;
    if (!snap) return;
    if (snap.data().simulated) {
      await transition(id, "TEST", { failureReason: "Simulated event; no provider send performed" });
      return;
    }
    if (!snap.data().dispatchEnabled) {
      const suppressed = Boolean(snap.data().cooldownSuppressed);
      const status = suppressed ? "SUPPRESSED" : "SKIPPED";
      await transition(id, status, {
        failureReason: suppressed ? null : "Notifications are disabled for this patient",
      });
      return;
    }
    if (!(await queueNewNotification(id))) return;
    await deliver(id, false);
  },
);

export const dispatchRetry = onDocumentUpdated(
  { document: "notifications/{notificationId}", secrets: [MSG91_AUTH_KEY] }, async (event) => {
    const before = event.data?.before.data();
    const after = event.data?.after.data();
    if (!before || !after || after.status !== "RETRYING" || before.status === "RETRYING") return;
    await appendStatus(event.params.notificationId, "RETRYING", "Manual retry requested");
    await deliver(event.params.notificationId, true);
  },
);

export const msg91DeliveryReceipt = onRequest({ secrets: [MSG91_DLR_SECRET] }, async (req, res) => {
  const supplied = String(req.query.key || "");
  const expected = MSG91_DLR_SECRET.value();
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    res.status(403).send("forbidden");
    return;
  }
  const requestId = String(req.body?.request_id || req.query.request_id || "");
  const state = String(req.body?.status || req.query.status || "").toLowerCase();
  if (!requestId || !["delivered", "delivrd", "success", "failed", "rejected", "undelivered"].includes(state)) {
    res.status(400).send("invalid receipt");
    return;
  }
  const found = await db.collection("notifications").where("msg91RequestId", "==", requestId).limit(1).get();
  if (found.empty) {
    res.status(404).send("notification not found");
    return;
  }
  const doc = found.docs[0];
  if (["delivered", "delivrd", "success"].includes(state)) {
    await transition(doc.id, "DELIVERED", { deliveredAt: new Date().toISOString(), failureReason: null });
  } else {
    await transition(doc.id, "FAILED", { failureReason: `MSG91 delivery receipt: ${state}` });
  }
  res.status(200).send("ok");
});
