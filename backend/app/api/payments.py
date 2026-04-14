"""
Payments API — backend/app/api/payments.py

The exception payment flow — used when a tenant has payment_required=True
(i.e. they have no booking engine of their own).

Flow:
  1. AI calls create_hold → gets hold_id + detects payment_required=True
  2. AI or guest frontend calls POST /payments/checkout/{hold_id}
  3. We create a Stripe Checkout Session and return the URL
  4. Guest completes payment on Stripe-hosted page
  5. Stripe calls POST /payments/webhook → we fire create_booking
  6. Confirmed booking lands in PMS, commission recorded

Two financial outcomes from every payment:
  - Commission amount  → stays in Vayancy's Stripe account
  - Net amount         → transferred to owner's Stripe Connect account

Mount in main.py:
  from app.api.payments import router as payments_router
  app.include_router(payments_router, prefix="/payments")

Requires in .env:
  STRIPE_SECRET_KEY=sk_live_...
  STRIPE_WEBHOOK_SECRET=whsec_...
"""
from __future__ import annotations

import json
import uuid
from datetime import date

import stripe
import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.session import get_pool
from app.mcp.adapters import WebHotelierAdapter

log    = APIRouter(tags=["payments"])
router = log
logger = structlog.get_logger()

# ── Stripe client ─────────────────────────────────────────────────────────────

def _stripe():
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = settings.stripe_secret_key
    return stripe


# ── POST /payments/checkout/{hold_id} ────────────────────────────────────────

@router.post("/checkout/{hold_id}")
async def create_checkout_session(
    hold_id:         str,
    idempotency_key: str,
    guest_name:      str,
    guest_phone:     str,
    request:         Request,
) -> dict:
    """
    Create a Stripe Checkout Session for a held booking.

    Called after create_hold when the tenant has payment_required=True.
    Returns a Stripe-hosted checkout URL for the guest to complete payment.

    The session amount = gross villa rate for the stay.
    Commission is split internally — guest pays one total, we distribute.

    Args:
        hold_id:         From create_hold
        idempotency_key: Caller-supplied — prevents double charge on retry
        guest_name:      For Stripe receipt
        guest_phone:     For booking confirmation
    """
    pool = await get_pool()
    s    = _stripe()

    # Load hold
    hold = await pool.fetchrow(
        """
        SELECT h.*, t.commission_pct, t.stripe_account_id,
               t.name AS tenant_name, t.pms_config, t.pms_type
        FROM   travelos_holds   h
        JOIN   travelos_tenants t ON t.id = h.tenant_id
        WHERE  h.id = $1 AND h.status = 'active' AND h.expires_at > NOW()
        """,
        hold_id,
    )
    if not hold:
        raise HTTPException(status_code=404,
                            detail="Hold not found, expired, or already used")

    # Check for existing session on this hold (idempotency)
    existing = await pool.fetchrow(
        "SELECT * FROM stripe_payments WHERE hold_id=$1 AND status='pending'",
        hold_id,
    )
    if existing:
        logger.info("stripe_session_idempotent", hold_id=hold_id)
        return {
            "checkout_url":   f"https://checkout.stripe.com/pay/{existing['stripe_session_id']}",
            "session_id":     existing["stripe_session_id"],
            "expires_at":     hold["expires_at"].isoformat(),
            "message":        "Existing checkout session returned",
        }

    # Calculate amounts
    pms_config    = json.loads(hold["pms_config"])
    adapter       = WebHotelierAdapter(
        api_key=pms_config["api_key"],
        property_id=pms_config["property_id"],
        api_base=pms_config.get("api_base", "https://api.webhotelier.net/v2"),
    )
    rate = await adapter.get_rate_details(
        unit_id=hold["unit_id"],
        check_in=hold["check_in"],
        check_out=hold["check_out"],
        guests=hold["guests"],
    )
    if rate.error:
        raise HTTPException(status_code=502,
                            detail=f"Could not get rate from PMS: {rate.error}")

    gross_total_eur  = rate.gross_total
    commission_pct   = float(hold["commission_pct"] or settings.travelos_commission_pct)
    commission_amt   = round(gross_total_eur * commission_pct / 100, 2)
    amount_cents     = int(gross_total_eur * 100)

    d1 = date.fromisoformat(hold["check_in"])
    d2 = date.fromisoformat(hold["check_out"])
    nights = (d2 - d1).days

    # Build Stripe session
    session_params: dict = {
        "payment_method_types": ["card"],
        "mode":                 "payment",
        "customer_email":       hold["guest_email"],
        # Dispute prevention #1: Force 3D Secure 2.0.
        # card.request_three_d_secure=any instructs Stripe to always request 3DS
        # when the card supports it. A successful 3DS challenge shifts liability
        # from us to the card issuer — the single most effective dispute prevention.
        "payment_method_options": {
            "card": {
                "request_three_d_secure": "any",
            },
        },
        "line_items": [{
            "price_data": {
                "currency":     rate.currency.lower(),
                "unit_amount":  amount_cents,
                "product_data": {
                    "name":        f"{nights}-night stay · {hold['check_in']} to {hold['check_out']}",
                    "description": f"Property unit {hold['unit_id']} · {hold['guests']} guests",
                },
            },
            "quantity": 1,
        }],
        "success_url": (
            f"{settings.travelos_success_url}?session_id={{CHECKOUT_SESSION_ID}}"
            f"&hold_id={hold_id}"
        ),
        "cancel_url":  f"{settings.travelos_cancel_url}?hold_id={hold_id}",
        "metadata": {
            "hold_id":            hold_id,
            "tenant_id":          str(hold["tenant_id"]),
            "idempotency_key":    idempotency_key,
            "guest_name":         guest_name,
            "guest_phone":        guest_phone,
            "commission_pct":     str(commission_pct),
            "commission_amt":     str(commission_amt),
            # Dispute prevention: store booking timestamp
            # (Stripe also records IP internally — visible in dashboard)
            "booked_at":          datetime.now(timezone.utc).isoformat(),
            "policies_presented": "cancellation,no_show,damage",
        },
        "expires_at": int(hold["expires_at"].timestamp()),

        # Dispute prevention #3: Policy consent shown at checkout.
        # Stripe displays this text above the Pay button — guest sees it
        # before confirming. The fact they completed payment after seeing
        # this text is evidence of informed consent.
        "custom_text": {
            "terms_of_service_acceptance": {
                "message": (
                    f"By completing payment I confirm I have read and agree to: "
                    f"(1) Cancellation policy — cancellations within 14 days of check-in are non-refundable. "
                    f"(2) No-show policy — full stay charged if guest does not arrive without notice. "
                    f"(3) Damage policy — guest is liable for damages beyond normal wear. "
                    f"Check-in: {hold['check_in']}. Check-out: {hold['check_out']}. "
                    f"Card will be charged on {hold['check_in']}."
                ),
            },
        },

        # Dispute prevention #4: Statement descriptor = property name.
        # Guest sees "VILLA AZURE MYKONOS" on their bank statement instead of
        # a generic processor name. They recognise the charge → don't dispute.
        # Max 22 chars for statement_descriptor_suffix.
        "payment_intent_data": {
            "statement_descriptor_suffix": (
                hold.get("tenant_name", "VAYANCY VILLAS")[:22].upper()
            ),
        },
    }

    # Dispute prevention #2: Pre-auth — authorize now, capture at check-in.
    # payment_intent_data already exists (set above with statement_descriptor_suffix).
    session_params["payment_intent_data"]["capture_method"] = "manual"

    # If owner has Stripe Connect, split payment at source
    if hold["stripe_account_id"]:
        session_params["payment_intent_data"].update({
            "application_fee_amount": int(commission_amt * 100),
            "transfer_data": {
                "destination": hold["stripe_account_id"],
            },
        })

    try:
        session = s.checkout.Session.create(**session_params)
    except stripe.error.StripeError as e:
        logger.error("stripe_session_failed", error=str(e), hold_id=hold_id)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message}")

    # Store session record + policy consent evidence
    client_ip = getattr(request.client, "host", "unknown") if hasattr(request, "client") else "unknown"
    await pool.execute(
        """
        INSERT INTO stripe_payments
            (hold_id, tenant_id, stripe_session_id, amount_total,
             commission_amount, currency, status, guest_email, idempotency_key, metadata)
        VALUES ($1,$2,$3,$4,$5,$6,'pending',$7,$8,$9)
        """,
        hold_id,
        hold["tenant_id"],
        session.id,
        gross_total_eur,
        commission_amt,
        rate.currency.lower(),
        hold["guest_email"],
        idempotency_key,
        json.dumps({
            "guest_name":  guest_name,
            "guest_phone": guest_phone,
            "nights":      nights,
            "unit_id":     hold["unit_id"],
        }),
    )

    # Store policy consent as evidence — IP + timestamp + policies shown
    await pool.execute(
        """
        INSERT INTO booking_evidence
            (booking_ref, property_id, guest_email, guest_phone,
             evidence_type, channel, content_summary, stripe_charge, metadata)
        VALUES ($1, $2, $3, $4, 'payment_receipt', 'stripe', $5, $6, $7)
        """,
        hold_id,
        str(hold["tenant_id"]),
        hold["guest_email"],
        guest_phone,
        (f"Stripe checkout session created. Policies presented: cancellation, "
         f"no-show, damage. Guest IP: {client_ip}. "
         f"Charge date shown: {hold['check_in']}."),
        session.id,
        json.dumps({
            "session_id":         session.id,
            "client_ip":          client_ip,
            "booked_at":          datetime.now(timezone.utc).isoformat(),
            "policies_presented": ["cancellation", "no_show", "damage"],
            "charge_date_shown":  hold["check_in"],
            "amount_shown":       gross_total_eur,
            "currency":           rate.currency,
        }),
    )

    logger.info("stripe_checkout_created",
                session_id=session.id, hold_id=hold_id,
                amount=gross_total_eur, commission=commission_amt)

    return {
        "checkout_url":    session.url,
        "session_id":      session.id,
        "amount_total":    gross_total_eur,
        "commission":      commission_amt,
        "currency":        rate.currency,
        "expires_at":      hold["expires_at"].isoformat(),
        "message":         "Guest must complete payment before hold expires.",
    }


# ── POST /payments/webhook ────────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
) -> JSONResponse:
    """
    Stripe webhook — fires after guest completes checkout.

    On checkout.session.completed:
      1. Verify Stripe signature (HMAC)
      2. Load hold from metadata
      3. Fire create_booking into PMS
      4. Mark stripe_payment as paid
      5. Mark hold as consumed

    On checkout.session.expired:
      1. Mark stripe_payment as expired
      2. Hold expires naturally via ARQ cron
    """
    body = await request.body()
    s    = _stripe()

    # Verify Stripe signature — never process unverified webhooks
    try:
        event = s.Webhook.construct_event(
            body, stripe_signature, settings.stripe_webhook_secret
        )
    except stripe.error.SignatureVerificationError:
        logger.warning("stripe_webhook_invalid_signature")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    pool = await get_pool()

    # ── checkout.session.completed ────────────────────────────────────────────
    if event["type"] == "checkout.session.completed":
        session  = event["data"]["object"]
        metadata = session.get("metadata", {})
        hold_id  = metadata.get("hold_id")
        idem_key = metadata.get("idempotency_key")

        if not hold_id or not idem_key:
            logger.error("stripe_webhook_missing_metadata", session_id=session["id"])
            return JSONResponse({"status": "ignored"})

        # Idempotency — don't process twice
        already = await pool.fetchrow(
            "SELECT id FROM stripe_payments WHERE stripe_session_id=$1 AND status='paid'",
            session["id"],
        )
        if already:
            return JSONResponse({"status": "already_processed"})

        # Load hold
        hold = await pool.fetchrow(
            """
            SELECT h.*, t.pms_config, t.pms_type
            FROM   travelos_holds   h
            JOIN   travelos_tenants t ON t.id = h.tenant_id
            WHERE  h.id = $1
            """,
            hold_id,
        )
        if not hold:
            logger.error("stripe_webhook_hold_not_found", hold_id=hold_id)
            return JSONResponse({"status": "hold_not_found"}, status_code=200)

        # Fire booking into PMS
        pms_config = json.loads(hold["pms_config"])
        adapter    = WebHotelierAdapter(
            api_key=pms_config["api_key"],
            property_id=pms_config["property_id"],
            api_base=pms_config.get("api_base", "https://api.webhotelier.net/v2"),
        )

        guest_name  = metadata.get("guest_name", "Guest")
        guest_phone = metadata.get("guest_phone", "")

        result = await adapter.create_booking(
            unit_id=hold["unit_id"],
            check_in=hold["check_in"],
            check_out=hold["check_out"],
            guests=hold["guests"],
            guest_name=guest_name,
            guest_email=hold["guest_email"],
            guest_phone=guest_phone,
            idempotency_key=idem_key,
            notes="Booked via Vayancy TravelOS — payment via Stripe",
            hold_ref=hold_id,
        )

        if result.success:
            # Record confirmed booking
            booking_id = str(uuid.uuid4())
            await pool.execute(
                """
                INSERT INTO travelos_bookings
                    (id, tenant_id, idempotency_key, pms_booking_id,
                     unit_id, check_in, check_out, guests,
                     guest_name, guest_email, guest_phone,
                     status, channel, commission_pct, notes)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                        'confirmed','travelos_stripe',$12,$13)
                """,
                booking_id,
                hold["tenant_id"],
                idem_key,
                result.pms_booking_id,
                hold["unit_id"],
                hold["check_in"],
                hold["check_out"],
                hold["guests"],
                guest_name,
                hold["guest_email"],
                guest_phone,
                float(metadata.get("commission_pct", settings.travelos_commission_pct)),
                "Payment collected via Stripe",
            )

            # Mark payment as paid
            await pool.execute(
                """
                UPDATE stripe_payments
                SET    status='paid',
                       stripe_payment_intent=$2,
                       paid_at=NOW()
                WHERE  stripe_session_id=$1
                """,
                session["id"],
                session.get("payment_intent"),
            )

            # Consume hold
            await pool.execute(
                "UPDATE travelos_holds SET status='consumed' WHERE id=$1", hold_id
            )

            logger.info("stripe_booking_confirmed",
                        booking_id=booking_id,
                        pms_id=result.pms_booking_id,
                        session_id=session["id"])
        else:
            logger.error("stripe_booking_failed_after_payment",
                         hold_id=hold_id, error=result.error,
                         session_id=session["id"])
            # Critical: payment collected but booking failed
            # Mark for manual review — do NOT silently fail
            await pool.execute(
                """
                UPDATE stripe_payments
                SET    status='paid', metadata = metadata || '{"pms_error": true}'::jsonb,
                       stripe_payment_intent=$2, paid_at=NOW()
                WHERE  stripe_session_id=$1
                """,
                session["id"],
                session.get("payment_intent"),
            )

    # ── checkout.session.expired ──────────────────────────────────────────────
    elif event["type"] == "checkout.session.expired":
        session = event["data"]["object"]
        await pool.execute(
            "UPDATE stripe_payments SET status='expired' WHERE stripe_session_id=$1",
            session["id"],
        )
        logger.info("stripe_session_expired", session_id=session["id"])

    # ── payment_intent.payment_failed ────────────────────────────────────────
    elif event["type"] == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        await pool.execute(
            "UPDATE stripe_payments SET status='failed' WHERE stripe_payment_intent=$1",
            pi["id"],
        )
        logger.warning("stripe_payment_failed", payment_intent=pi["id"])

    return JSONResponse({"status": "ok"})


# ── POST /payments/capture/{booking_id} ─────────────────────────────────────
# Called on check-in day to capture the pre-authorized amount.
# Until this is called, the guest's card shows a hold but no actual charge.

@router.post("/capture/{booking_id}")
async def capture_payment(booking_id: str) -> dict:
    """
    Capture a pre-authorized payment at check-in.
    Call this on check-in day (or trigger from operations agent on guest.checkin event).

    Until capture, the guest sees a hold on their card but no charge.
    This is proof the card was valid at booking — strengthens dispute defense.
    """
    pool = await get_pool()
    s    = _stripe()

    # Get the payment intent from stripe_payments
    row = await pool.fetchrow(
        """
        SELECT sp.stripe_payment_intent, sp.amount_total, sp.currency,
               sp.tenant_id, sp.guest_email, b.pms_booking_id
        FROM   stripe_payments sp
        JOIN   travelos_bookings b ON b.idempotency_key = sp.idempotency_key
        WHERE  b.id = $1 AND sp.status = 'paid'
        """,
        booking_id,
    )
    if not row or not row["stripe_payment_intent"]:
        raise HTTPException(status_code=404,
                            detail="No capturable payment found for this booking")

    try:
        pi = s.PaymentIntent.capture(row["stripe_payment_intent"])
        logger.info("payment_captured",
                    booking_id=booking_id,
                    payment_intent=pi.id,
                    amount=row["amount_total"])

        # Store capture as evidence
        await pool.execute(
            """
            INSERT INTO booking_evidence
                (booking_ref, property_id, guest_email, evidence_type,
                 channel, content_summary, stripe_charge, metadata)
            VALUES ($1, 'unknown', $2, 'preauth_captured', 'stripe',
                    $3, $4, $5)
            """,
            booking_id,
            row["guest_email"],
            f"Pre-auth captured at check-in: {row['currency'].upper()} {row['amount_total']}",
            pi.latest_charge,
            json.dumps({"payment_intent": pi.id, "captured_at": "check-in"}),
        )

        # Send payment receipt email
        try:
            from app.core.email import send_payment_receipt_email
            receipt_url = f"https://dashboard.stripe.com/payments/{pi.id}"
            email_msg_id = await send_payment_receipt_email(
                to=row["guest_email"],
                guest_name="Guest",
                property_name=row.get("property_name", "your villa"),
                check_in="",
                check_out="",
                amount=float(row["amount_total"]),
                currency=row["currency"].upper(),
                charge_id=pi.latest_charge or pi.id,
                booking_ref=booking_id,
                stripe_receipt=receipt_url,
            )
            if email_msg_id:
                await pool.execute(
                    """
                    INSERT INTO booking_evidence
                        (booking_ref, property_id, guest_email,
                         evidence_type, channel, content_summary,
                         stripe_charge, metadata)
                    VALUES ($1, 'unknown', $2, 'payment_receipt',
                            'email', $3, $4, $5)
                    """,
                    booking_id, row["guest_email"],
                    f"Payment receipt emailed: {row['currency'].upper()} {row['amount_total']}",
                    pi.latest_charge,
                    json.dumps({"postmark_message_id": email_msg_id,
                                "payment_intent": pi.id}),
                )
        except Exception as e:
            logger.warning("receipt_email_failed", booking_id=booking_id, error=str(e))

        return {
            "booking_id":      booking_id,
            "payment_intent":  pi.id,
            "amount_captured": float(row["amount_total"]),
            "currency":        row["currency"],
            "status":          "captured",
            "receipt_emailed": True,
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── GET /payments/status/{hold_id} ────────────────────────────────────────────

@router.get("/status/{hold_id}")
async def payment_status(hold_id: str) -> dict:
    """
    Poll payment status for a hold. Used by AI to check if guest has paid.
    Returns status: pending | paid | expired | failed
    """
    pool = await get_pool()
    row  = await pool.fetchrow(
        """
        SELECT status, amount_total, commission_amount,
               currency, paid_at, stripe_session_id
        FROM   stripe_payments
        WHERE  hold_id = $1
        ORDER  BY created_at DESC
        LIMIT  1
        """,
        hold_id,
    )
    if not row:
        return {"hold_id": hold_id, "status": "no_session"}

    return {
        "hold_id":    hold_id,
        "status":     row["status"],
        "amount":     float(row["amount_total"]),
        "commission": float(row["commission_amount"]),
        "currency":   row["currency"],
        "paid_at":    row["paid_at"].isoformat() if row["paid_at"] else None,
    }
