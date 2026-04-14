"""
Email — backend/app/core/email.py

Sends transactional emails via Postmark API (no SDK needed — pure HTTP).

Why Postmark over SendGrid:
  - Purpose-built for transactional email (receipts, confirmations)
  - Better deliverability for one-to-one triggered emails
  - Simpler API — one endpoint, one key
  - Free tier covers 100 emails/month for testing

Emails sent:
  1. Booking confirmation — immediately after create_booking()
     Contains: dates, property, booking ref, ALL policies agreed,
     charge date, Stripe receipt link (if applicable).
     This is the "you agreed to X policy" email requested for dispute defense.

  2. Pre-arrival reminder — 3 days before check-in (via cron)
     Contains: check-in time, charge date reminder, property contact.

  3. Payment receipt — after Stripe capture on check-in day
     Contains: exact amount charged, charge ID, receipt URL.

Dispute defense value:
  Every email is stored in booking_evidence with a Postmark MessageID.
  Postmark's MessageID is their delivery record — proves we sent it.
  Combined with WhatsApp confirmation, it shows the guest received
  the policy information on two separate channels.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

POSTMARK_API = "https://api.postmarkapp.com/email"


# ── HTML templates ────────────────────────────────────────────────────────────

def _html_booking_confirmation(
    guest_name:     str,
    property_name:  str,
    check_in:       str,
    check_out:      str,
    guests:         int,
    booking_ref:    str,
    charge_date:    str,
    total_amount:   float | None = None,
    currency:       str = "EUR",
    stripe_receipt: str | None = None,
) -> tuple[str, str]:
    """Returns (subject, html_body)."""
    amount_row = (
        f"<tr><td><b>Amount</b></td><td>{currency} {total_amount:.2f}</td></tr>"
        if total_amount else ""
    )
    receipt_row = (
        f'<tr><td><b>Payment receipt</b></td>'
        f'<td><a href="{stripe_receipt}">View receipt</a></td></tr>'
        if stripe_receipt else ""
    )
    charge_notice = (
        f'<p style="background:#fff8e1;padding:12px;border-left:4px solid #f59e0b">'
        f'<b>Your card will be charged on {charge_date}.</b><br>'
        f'This is the date you authorised at booking.</p>'
        if charge_date else ""
    )

    subject = f"Booking Confirmed — {property_name} | Ref {booking_ref[:8].upper()}"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Booking Confirmation</title></head>
<body style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">

  <div style="background:#0a0a0a;padding:20px;border-radius:8px 8px 0 0">
    <h1 style="color:#c8a96e;margin:0;font-size:22px">Vayancy</h1>
    <p style="color:#888;margin:4px 0 0">Luxury Villa Management</p>
  </div>

  <div style="border:1px solid #e5e5e5;border-top:none;padding:24px;border-radius:0 0 8px 8px">

    <h2 style="color:#1a1a1a">Your booking is confirmed</h2>
    <p>Hi {guest_name},</p>
    <p>Thank you for booking with Vayancy. Here are your reservation details:</p>

    <table style="width:100%;border-collapse:collapse;margin:16px 0">
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666;width:40%"><b>Booking reference</b></td><td style="padding:8px 0"><b>{booking_ref[:8].upper()}</b></td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Property</b></td><td style="padding:8px 0">{property_name}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Check-in</b></td><td style="padding:8px 0">{check_in}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Check-out</b></td><td style="padding:8px 0">{check_out}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Guests</b></td><td style="padding:8px 0">{guests}</td></tr>
      {amount_row}
      {receipt_row}
    </table>

    {charge_notice}

    <div style="background:#f9f9f9;padding:16px;border-radius:6px;margin:20px 0">
      <h3 style="margin:0 0 12px;font-size:15px">Policies you agreed to at booking</h3>
      <p style="margin:0 0 8px;font-size:14px">
        <b>Cancellation policy:</b> Cancellations within 14 days of check-in are non-refundable.
        Cancellations more than 14 days before check-in receive a full refund.
      </p>
      <p style="margin:0 0 8px;font-size:14px">
        <b>No-show policy:</b> If you do not arrive on the check-in date without prior notice,
        the full stay will be charged.
      </p>
      <p style="margin:0;font-size:14px">
        <b>Damage policy:</b> Guests are responsible for any damage to the property
        beyond normal wear and tear during their stay.
      </p>
    </div>

    <p style="font-size:13px;color:#666">
      By completing your booking you confirmed that you read and agreed to all policies above.
      This email serves as your record of that agreement, timestamped at
      {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}.
    </p>

    <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
    <p style="font-size:12px;color:#999;margin:0">
      Questions? Reply to this email or message us on WhatsApp.<br>
      Vayancy · bookings@vayancy.gr
    </p>

  </div>
</body>
</html>"""

    return subject, html


def _html_pre_arrival(
    guest_name:    str,
    property_name: str,
    check_in:      str,
    charge_date:   str,
    total_amount:  float | None,
    currency:      str = "EUR",
) -> tuple[str, str]:
    amount_str = f"{currency} {total_amount:.2f}" if total_amount else "the agreed amount"
    subject    = f"Your stay at {property_name} is in 3 days"
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:#0a0a0a;padding:20px;border-radius:8px 8px 0 0">
    <h1 style="color:#c8a96e;margin:0;font-size:22px">Vayancy</h1>
  </div>
  <div style="border:1px solid #e5e5e5;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <h2>Your stay is coming up!</h2>
    <p>Hi {guest_name},</p>
    <p>Just a reminder that your stay at <b>{property_name}</b> begins on <b>{check_in}</b>.</p>
    <div style="background:#fff8e1;padding:12px;border-left:4px solid #f59e0b;margin:16px 0">
      <b>{amount_str} will be charged to your card on {charge_date}.</b><br>
      This is the amount you authorised when you made your booking.
    </div>
    <p>If you have any questions or need to make changes, reply to this email
    or message us on WhatsApp.</p>
    <p>We look forward to welcoming you.</p>
  </div>
</body>
</html>"""
    return subject, html


def _html_payment_receipt(
    guest_name:      str,
    property_name:   str,
    check_in:        str,
    check_out:       str,
    amount:          float,
    currency:        str,
    charge_id:       str,
    booking_ref:     str,
    stripe_receipt:  str | None = None,
) -> tuple[str, str]:
    receipt_link = (
        f'<a href="{stripe_receipt}" style="color:#c8a96e">View Stripe receipt</a>'
        if stripe_receipt else charge_id
    )
    subject = f"Payment receipt — {property_name}"
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:#0a0a0a;padding:20px;border-radius:8px 8px 0 0">
    <h1 style="color:#c8a96e;margin:0;font-size:22px">Vayancy</h1>
  </div>
  <div style="border:1px solid #e5e5e5;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <h2>Payment confirmed</h2>
    <p>Hi {guest_name}, your payment has been processed.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0">
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666;width:40%"><b>Property</b></td><td style="padding:8px 0">{property_name}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Check-in</b></td><td style="padding:8px 0">{check_in}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Check-out</b></td><td style="padding:8px 0">{check_out}</td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Amount charged</b></td><td style="padding:8px 0"><b>{currency.upper()} {amount:.2f}</b></td></tr>
      <tr style="border-bottom:1px solid #eee"><td style="padding:8px 0;color:#666"><b>Booking ref</b></td><td style="padding:8px 0">{booking_ref[:8].upper()}</td></tr>
      <tr><td style="padding:8px 0;color:#666"><b>Receipt</b></td><td style="padding:8px 0">{receipt_link}</td></tr>
    </table>
    <p style="font-size:13px;color:#666">
      This charge will appear on your statement as the property name.
      Keep this email as your payment record.
    </p>
  </div>
</body>
</html>"""
    return subject, html


# ── Sender ────────────────────────────────────────────────────────────────────

async def send_email(
    to:       str,
    subject:  str,
    html:     str,
    tag:      str = "booking",
) -> str | None:
    """
    Send email via Postmark. Returns Postmark MessageID or None on failure.
    MessageID is stored as evidence — it's Postmark's delivery record.

    No-op if POSTMARK_API_KEY is not set (dev/test environments).
    """
    if not settings.postmark_api_key:
        log.warning("email_not_sent_no_postmark_key", to=to, subject=subject)
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                POSTMARK_API,
                headers={
                    "Accept":                  "application/json",
                    "Content-Type":            "application/json",
                    "X-Postmark-Server-Token": settings.postmark_api_key,
                },
                json={
                    "From":     f"{settings.email_from_name} <{settings.email_from_address}>",
                    "To":       to,
                    "ReplyTo":  settings.email_reply_to,
                    "Subject":  subject,
                    "HtmlBody": html,
                    "Tag":      tag,
                    "TrackOpens": True,
                    "MessageStream": "outbound",
                },
            )
            r.raise_for_status()
            msg_id = r.json().get("MessageID")
            log.info("email_sent", to=to, subject=subject, message_id=msg_id)
            return msg_id
    except Exception as e:
        log.error("email_failed", to=to, subject=subject, error=str(e))
        return None


# ── Convenience functions called from dispute_defense and payments ────────────

async def send_booking_confirmation_email(
    to:             str,
    guest_name:     str,
    property_name:  str,
    check_in:       str,
    check_out:      str,
    guests:         int,
    booking_ref:    str,
    charge_date:    str,
    total_amount:   float | None = None,
    currency:       str = "EUR",
    stripe_receipt: str | None = None,
) -> str | None:
    subject, html = _html_booking_confirmation(
        guest_name=guest_name, property_name=property_name,
        check_in=check_in, check_out=check_out, guests=guests,
        booking_ref=booking_ref, charge_date=charge_date,
        total_amount=total_amount, currency=currency,
        stripe_receipt=stripe_receipt,
    )
    return await send_email(to, subject, html, tag="booking-confirmation")


async def send_pre_arrival_email(
    to:            str,
    guest_name:    str,
    property_name: str,
    check_in:      str,
    charge_date:   str,
    total_amount:  float | None = None,
    currency:      str = "EUR",
) -> str | None:
    subject, html = _html_pre_arrival(
        guest_name=guest_name, property_name=property_name,
        check_in=check_in, charge_date=charge_date,
        total_amount=total_amount, currency=currency,
    )
    return await send_email(to, subject, html, tag="pre-arrival")


async def send_payment_receipt_email(
    to:             str,
    guest_name:     str,
    property_name:  str,
    check_in:       str,
    check_out:      str,
    amount:         float,
    currency:       str,
    charge_id:      str,
    booking_ref:    str,
    stripe_receipt: str | None = None,
) -> str | None:
    subject, html = _html_payment_receipt(
        guest_name=guest_name, property_name=property_name,
        check_in=check_in, check_out=check_out,
        amount=amount, currency=currency,
        charge_id=charge_id, booking_ref=booking_ref,
        stripe_receipt=stripe_receipt,
    )
    return await send_email(to, subject, html, tag="payment-receipt")
