"""
Dispute Defense — backend/app/api/dispute_defense.py

Works for BOTH payment flows (invoice monthly + Stripe checkout).

Two mechanisms:
  1. Booking contract via WhatsApp — sent after every confirmed booking.
     Guest receives booking summary + T&Cs and must reply YES.
     Their reply (with WhatsApp message ID + timestamp) is stored as legal proof.

  2. Dispute defense report — one endpoint that assembles all timestamped
     evidence for a booking. When Stripe sends a chargeback, you submit this.

What gets stored as evidence:
  - The contract message we sent (WA message ID + exact timestamp)
  - The guest's YES reply (WA message ID + timestamp — proves receipt)
  - Check-in instructions delivered (WA message ID + timestamp)
  - Payment receipt (Stripe charge ID)
  - Pre-auth capture timestamp (if applicable)
  - Every WhatsApp interaction during the stay

Why this works:
  Stripe's dispute process accepts evidence documents. A timestamped WhatsApp
  message showing the guest received and confirmed the booking terms, followed
  by a second timestamped message showing they received check-in instructions,
  is strong evidence against "I never made this booking" disputes.
  WhatsApp Business API provides cryptographic message IDs — these are not
  forgeable and are accepted as delivery proof.

Mount in main.py:
  from app.api.dispute_defense import router as dispute_router
  app.include_router(dispute_router, prefix="/dispute")
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.config import settings
from app.db.session import get_pool

router  = APIRouter(tags=["dispute_defense"])
_bearer = HTTPBearer(auto_error=False)
logger  = structlog.get_logger()


def _require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── WhatsApp helper ───────────────────────────────────────────────────────────

async def _send_whatsapp(to: str, body: str) -> str | None:
    """Send WhatsApp message, return message ID or None on failure."""
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        logger.warning("whatsapp_not_configured_for_contract")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{settings.whatsapp_api_base}/{settings.whatsapp_phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {settings.whatsapp_access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to":   to,
                    "type": "text",
                    "text": {"body": body, "preview_url": False},
                },
            )
            r.raise_for_status()
            msg_id = r.json().get("messages", [{}])[0].get("id")
            logger.info("contract_message_sent", to=to, message_id=msg_id)
            return msg_id
    except Exception as e:
        logger.error("contract_message_failed", to=to, error=str(e))
        return None


# ── Contract message builder ──────────────────────────────────────────────────

def _build_contract_message(
    guest_name:     str,
    property_name:  str,
    check_in:       str,
    check_out:      str,
    guests:         int,
    booking_ref:    str,
    total_amount:   float | None = None,
    currency:       str = "EUR",
) -> str:
    amount_line = (
        f"Total: {currency} {total_amount:.2f}\n"
        if total_amount else ""
    )
    return (
        f"Hi {guest_name}, your booking is confirmed.\n\n"
        f"*Booking reference:* {booking_ref}\n"
        f"*Property:* {property_name}\n"
        f"*Check-in:* {check_in}\n"
        f"*Check-out:* {check_out}\n"
        f"*Guests:* {guests}\n"
        f"{amount_line}\n"
        f"*Terms & Conditions:*\n"
        f"By replying YES to this message you confirm:\n"
        f"• You made this booking intentionally\n"
        f"• You agree to the property's cancellation policy\n"
        f"• The booking details above are correct\n"
        f"• You authorise the associated charge to your payment method\n\n"
        f"Please reply *YES* to confirm. This message is legally binding "
        f"and timestamped."
    )


# ── Schemas ───────────────────────────────────────────────────────────────────

class SendContractRequest(BaseModel):
    booking_ref:    str
    property_id:    str
    property_name:  str
    guest_name:     str
    guest_email:    str
    guest_phone:    str
    check_in:       str
    check_out:      str
    guests:         int
    total_amount:   float | None = None
    currency:       str = "EUR"


class RecordEvidenceRequest(BaseModel):
    booking_ref:    str
    property_id:    str
    guest_email:    str
    guest_phone:    str | None = None
    evidence_type:  str
    content_summary: str
    wa_message_id:  str | None = None
    stripe_charge:  str | None = None
    metadata:       dict = {}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/send-contract", dependencies=[Depends(_require_auth)])
async def send_booking_contract(body: SendContractRequest) -> dict:
    """
    Send the booking confirmation + T&Cs contract via WhatsApp.

    Call this immediately after every confirmed booking — both the default
    invoice flow and the Stripe checkout flow.

    The WhatsApp message ID returned by Meta's API is the cryptographic proof
    the message was delivered. Store it. When the guest replies YES, that
    reply's message ID proves acceptance.

    The guest's YES reply is caught by the existing WhatsApp webhook →
    webhooks.py → stored in guest_interactions. The dispute defense report
    assembles both sides of this exchange.
    """
    pool = await get_pool()

    message = _build_contract_message(
        guest_name=body.guest_name,
        property_name=body.property_name,
        check_in=body.check_in,
        check_out=body.check_out,
        guests=body.guests,
        booking_ref=body.booking_ref,
        total_amount=body.total_amount,
        currency=body.currency,
    )

    wa_msg_id = await _send_whatsapp(body.guest_phone, message)

    # Store as evidence regardless of whether WA delivery succeeded
    evidence_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO booking_evidence
            (id, booking_ref, property_id, guest_email, guest_phone,
             evidence_type, channel, content_summary, wa_message_id, metadata)
        VALUES ($1,$2,$3,$4,$5,'booking_contract','whatsapp',$6,$7,$8)
        """,
        evidence_id,
        body.booking_ref,
        body.property_id,
        body.guest_email,
        body.guest_phone,
        f"Contract sent to {body.guest_phone} for {body.property_name} "
        f"{body.check_in}→{body.check_out}",
        wa_msg_id,
        json.dumps({
            "property_name": body.property_name,
            "check_in":      body.check_in,
            "check_out":     body.check_out,
            "guests":        body.guests,
            "total_amount":  body.total_amount,
            "currency":      body.currency,
            "delivered":     wa_msg_id is not None,
        }),
    )

    logger.info("contract_evidence_stored",
                booking_ref=body.booking_ref,
                wa_delivered=wa_msg_id is not None,
                evidence_id=evidence_id)

    return {
        "evidence_id":  evidence_id,
        "wa_message_id": wa_msg_id,
        "delivered":    wa_msg_id is not None,
        "next":         "Guest must reply YES. Their reply is auto-stored via webhook.",
        "message":      "Contract sent. Evidence recorded.",
    }


@router.post("/record-acceptance", dependencies=[Depends(_require_auth)])
async def record_guest_acceptance(
    booking_ref:  str,
    property_id:  str,
    guest_email:  str,
    guest_phone:  str,
    wa_message_id: str,  # the message ID of the guest's YES reply
) -> dict:
    """
    Record the guest's YES reply as contract acceptance evidence.

    Called by the WhatsApp webhook handler when the guest replies YES
    to the booking contract message. The WA message ID of their reply
    is the proof of acceptance — timestamped by Meta's infrastructure,
    not by us.
    """
    pool = await get_pool()
    evidence_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO booking_evidence
            (id, booking_ref, property_id, guest_email, guest_phone,
             evidence_type, channel, content_summary, wa_message_id, metadata)
        VALUES ($1,$2,$3,$4,$5,'contract_accepted','whatsapp',$6,$7,$8)
        """,
        evidence_id,
        booking_ref,
        property_id,
        guest_email,
        guest_phone,
        f"Guest replied YES to booking contract",
        wa_message_id,
        json.dumps({
            "acceptance_method": "whatsapp_reply",
            "accepted_at":       datetime.now(timezone.utc).isoformat(),
        }),
    )
    logger.info("contract_accepted", booking_ref=booking_ref,
                guest_phone=guest_phone, wa_id=wa_message_id)
    return {"evidence_id": evidence_id, "status": "acceptance_recorded"}


@router.post("/record-evidence", dependencies=[Depends(_require_auth)])
async def record_evidence(body: RecordEvidenceRequest) -> dict:
    """
    Manually record any piece of evidence for a booking.
    Use for: check-in instructions sent, payment receipts, any guest contact.
    """
    pool = await get_pool()
    evidence_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO booking_evidence
            (id, booking_ref, property_id, guest_email, guest_phone,
             evidence_type, channel, content_summary, wa_message_id,
             stripe_charge, metadata)
        VALUES ($1,$2,$3,$4,$5,$6,'whatsapp',$7,$8,$9,$10)
        """,
        evidence_id,
        body.booking_ref,
        body.property_id,
        body.guest_email,
        body.guest_phone,
        body.evidence_type,
        body.content_summary,
        body.wa_message_id,
        body.stripe_charge,
        json.dumps(body.metadata),
    )
    return {"evidence_id": evidence_id, "status": "recorded"}


# ── Message templates ────────────────────────────────────────────────────────

def _booking_confirmation_message(
    guest_name: str, property_name: str, check_in: str, check_out: str,
    guests: int, booking_ref: str, charge_date: str, total_amount: float | None,
    currency: str = "EUR",
) -> str:
    amount_line = (f"Amount: {currency} {total_amount:.2f}\n") if total_amount else ""
    return (
        f"Hi {guest_name}! Your booking is confirmed.\n\n"
        f"Property: {property_name}\n"
        f"Check-in: {check_in}\n"
        f"Check-out: {check_out}\n"
        f"Guests: {guests}\n"
        f"{amount_line}"
        f"Booking ref: {booking_ref}\n\n"
        f"Your card will be charged on *{charge_date}*.\n\n"
        f"You agreed to our cancellation, no-show and damage policies at booking.\n"
        f"Questions? Reply to this message anytime."
    )


def _pre_arrival_reminder_message(
    guest_name: str, property_name: str, check_in: str,
    charge_date: str, total_amount: float | None, currency: str = "EUR",
) -> str:
    amount_line = f"{currency} {total_amount:.2f}" if total_amount else "the agreed amount"
    return (
        f"Hi {guest_name}, your stay at {property_name} is coming up!\n\n"
        f"Check-in: *{check_in}*\n\n"
        f"Reminder: *{amount_line} will be charged to your card on {charge_date}.*\n\n"
        f"If you have any questions or need to make changes please reply here. "
        f"We look forward to welcoming you."
    )


def _charge_notice_message(
    guest_name: str, property_name: str,
    amount: float, currency: str, charge_date: str,
) -> str:
    return (
        f"Hi {guest_name}, just a heads up — "
        f"your payment of *{currency} {amount:.2f}* for {property_name} "
        f"will be processed today ({charge_date}).\n\n"
        f"This is the amount you authorised at booking. "
        f"You will receive a receipt from Stripe shortly."
    )


# ── POST /dispute/send-messages ───────────────────────────────────────────────

class SendScheduledMessagesRequest(BaseModel):
    booking_ref:   str
    property_id:   str
    property_name: str
    guest_name:    str
    guest_email:   str
    guest_phone:   str
    check_in:      str
    check_out:     str
    guests:        int
    charge_date:   str       # check-in date for pre-auth, or booking date for immediate charge
    total_amount:  float | None = None
    currency:      str = "EUR"
    message_type:  str        # "booking_confirmation" | "pre_arrival" | "charge_notice"


@router.post("/send-messages", dependencies=[Depends(_require_auth)])
async def send_scheduled_message(body: SendScheduledMessagesRequest) -> dict:
    """
    Send a timed dispute-prevention message via WhatsApp.

    Three message types:
      booking_confirmation — sent immediately after booking confirmed.
                             Includes charge date, policies agreed, booking ref.
      pre_arrival          — sent 3 days before check-in.
                             Reminds guest of charge date and amount.
      charge_notice        — sent on check-in day before capture.
                             "Your card will be charged today."

    Each is stored as evidence with WA message ID.

    Call from:
      - Booking confirmation: immediately after create_booking()
      - Pre-arrival: scheduled cron job (see worker.py)
      - Charge notice: operations agent on check-in event
    """
    pool = await get_pool()

    if body.message_type == "booking_confirmation":
        evidence_type = "booking_contract"
        message = _booking_confirmation_message(
            guest_name=body.guest_name, property_name=body.property_name,
            check_in=body.check_in, check_out=body.check_out,
            guests=body.guests, booking_ref=body.booking_ref,
            charge_date=body.charge_date,
            total_amount=body.total_amount, currency=body.currency,
        )
    elif body.message_type == "pre_arrival":
        evidence_type = "communication"
        message = _pre_arrival_reminder_message(
            guest_name=body.guest_name, property_name=body.property_name,
            check_in=body.check_in, charge_date=body.charge_date,
            total_amount=body.total_amount, currency=body.currency,
        )
    elif body.message_type == "charge_notice":
        evidence_type = "communication"
        message = _charge_notice_message(
            guest_name=body.guest_name, property_name=body.property_name,
            amount=body.total_amount or 0, currency=body.currency,
            charge_date=body.charge_date,
        )
    else:
        raise HTTPException(status_code=400,
                            detail="message_type must be booking_confirmation, pre_arrival, or charge_notice")

    wa_msg_id = await _send_whatsapp(body.guest_phone, message)

    evidence_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO booking_evidence
            (id, booking_ref, property_id, guest_email, guest_phone,
             evidence_type, channel, content_summary, wa_message_id, metadata)
        VALUES ($1,$2,$3,$4,$5,$6,'whatsapp',$7,$8,$9)
        """,
        evidence_id, body.booking_ref, body.property_id,
        body.guest_email, body.guest_phone, evidence_type,
        f"{body.message_type} sent to {body.guest_phone}",
        wa_msg_id,
        json.dumps({
            "message_type": body.message_type,
            "charge_date":  body.charge_date,
            "amount":       body.total_amount,
            "delivered":    wa_msg_id is not None,
        }),
    )

    return {
        "evidence_id":  evidence_id,
        "message_type": body.message_type,
        "wa_message_id": wa_msg_id,
        "delivered":    wa_msg_id is not None,
    }


@router.get("/report/{booking_ref}", dependencies=[Depends(_require_auth)])
async def dispute_defense_report(booking_ref: str) -> dict:
    """
    Assemble the full dispute defense package for a booking.

    Returns all timestamped evidence in chronological order.
    Submit this to Stripe when responding to a chargeback.

    The report includes:
      - Booking contract sent (WA message ID + timestamp)
      - Guest acceptance (their YES reply WA message ID + timestamp)
      - Check-in instructions delivered
      - Payment receipt / pre-auth capture
      - All WhatsApp interactions during the stay
      - PMS booking record reference
    """
    pool = await get_pool()

    # Booking evidence records
    evidence = await pool.fetch(
        """
        SELECT evidence_type, channel, content_summary,
               wa_message_id, stripe_charge, metadata, created_at
        FROM   booking_evidence
        WHERE  booking_ref = $1
        ORDER  BY created_at ASC
        """,
        booking_ref,
    )

    # WhatsApp interaction history (from guest_interactions)
    # Find guest email/phone from evidence
    booking_email = None
    booking_phone = None
    if evidence:
        booking_email = evidence[0]["metadata"]
        first = await pool.fetchrow(
            "SELECT guest_email, guest_phone FROM booking_evidence WHERE booking_ref=$1 LIMIT 1",
            booking_ref,
        )
        if first:
            booking_email = first["guest_email"]
            booking_phone = first["guest_phone"]

    interactions = []
    if booking_phone:
        rows = await pool.fetch(
            """
            SELECT direction, content, channel, created_at
            FROM   guest_interactions
            WHERE  guest_phone = $1
              AND  reservation_id = $2
               OR  (guest_phone = $1 AND reservation_id IS NULL)
            ORDER  BY created_at ASC
            LIMIT  100
            """,
            booking_phone, booking_ref,
        )
        interactions = [
            {
                "direction":  r["direction"],
                "channel":    r["channel"],
                "summary":    r["content"][:200],
                "timestamp":  r["created_at"].isoformat(),
            }
            for r in rows
        ]

    # Stripe payment record
    stripe_records = await pool.fetch(
        """
        SELECT stripe_session_id, stripe_payment_intent,
               amount_total, commission_amount, currency,
               status, paid_at, created_at
        FROM   stripe_payments sp
        JOIN   travelos_bookings b ON b.idempotency_key = sp.idempotency_key
        WHERE  b.id = $1 OR b.pms_booking_id = $1
        """,
        booking_ref,
    )

    # Score the strength of the defense
    evidence_types = {e["evidence_type"] for e in evidence}
    score = 0
    checklist = []

    # Count channels per evidence type — WhatsApp + email = stronger
    channels_by_type: dict[str, set] = {}
    for e in evidence:
        t = e["evidence_type"]
        if t not in channels_by_type:
            channels_by_type[t] = set()
        channels_by_type[t].add(e["channel"])

    checks = [
        ("booking_contract",     "Contract sent via WhatsApp",           20),
        ("booking_contract",     "Confirmation email with policies sent", 15),  # checks email channel
        ("contract_accepted",    "Guest replied YES to WhatsApp",         30),
        ("checkin_instructions", "Check-in instructions delivered",       15),
        ("payment_receipt",      "Payment receipt sent",                  10),
        ("preauth_captured",     "Pre-auth captured at check-in",         10),
    ]

    seen = set()
    for etype, label, points in checks:
        if etype not in evidence_types:
            checklist.append({"check": label, "present": False, "points": 0})
            continue
        # For booking_contract check email separately
        if etype == "booking_contract" and label.startswith("Confirmation email"):
            present = "email" in channels_by_type.get("booking_contract", set())
        else:
            key = (etype, label)
            present = etype in evidence_types and key not in seen
            seen.add(key)

        checklist.append({"check": label, "present": present,
                          "points": points if present else 0})
        if present:
            score += points

    if score >= 80:
        strength = "strong"
    elif score >= 50:
        strength = "moderate"
    else:
        strength = "weak — send contract and record acceptance before dispute"

    return {
        "booking_ref":       booking_ref,
        "defense_score":     score,
        "defense_strength":  strength,
        "checklist":         checklist,
        "evidence_timeline": [
            {
                "type":        e["evidence_type"],
                "channel":     e["channel"],
                "summary":     e["content_summary"],
                "wa_msg_id":   e["wa_message_id"],
                "stripe_charge": e["stripe_charge"],
                "timestamp":   e["created_at"].isoformat(),
            }
            for e in evidence
        ],
        "whatsapp_interactions": interactions,
        "stripe_payments": [
            {
                "session_id":      r["stripe_session_id"],
                "payment_intent":  r["stripe_payment_intent"],
                "amount":          float(r["amount_total"]),
                "commission":      float(r["commission_amount"]),
                "currency":        r["currency"],
                "status":          r["status"],
                "paid_at":         r["paid_at"].isoformat() if r["paid_at"] else None,
            }
            for r in stripe_records
        ],
        "total_evidence_items": len(evidence),
        "total_interactions":   len(interactions),
    }
