from __future__ import annotations
from pydantic import BaseModel, Field
from datetime import datetime, date
from typing import Literal, Any
import uuid


class WorkflowLog(BaseModel):
    id:          str
    timestamp:   datetime
    agent:       Literal["supervisor", "guest", "revenue", "operations"]
    action:      str
    status:      Literal["thinking", "tool_call", "completed", "error"]
    details:     str
    workflow_id: str


class GuestProfile(BaseModel):
    id:          str
    name:        str
    email:       str | None = None
    phone:       str | None = None
    nationality: str | None = None
    language:    str = "en"
    notes:       str | None = None


class Reservation(BaseModel):
    id:           str
    property_id:  str
    source:       str
    status:       str
    checkin:      date
    checkout:     date
    guests_adult: int
    guests_child: int = 0
    guest:        GuestProfile
    total_amount: float
    currency:     str = "EUR"
    notes:        str | None = None


class BookingEventType(str):
    CONFIRMED = "booking.confirmed"
    MODIFIED  = "booking.modified"
    CANCELLED = "booking.cancelled"
    NO_SHOW   = "booking.noshow"
    CHECKIN   = "guest.checkin"
    CHECKOUT  = "guest.checkout"


# ── TravelOS models ───────────────────────────────────────────────────────────

class TravelOSTenant(BaseModel):
    id:         str
    name:       str
    pms_type:   str
    active:     bool
    created_at: datetime


class TravelOSProperty(BaseModel):
    id:          str
    tenant_id:   str
    pms_unit_id: str
    name:        str
    location:    str
    region:      str | None = None
    max_guests:  int
    bedrooms:    int = 1
    bathrooms:   int = 1
    amenities:   list[str] = []
    base_rate:   float | None = None
    currency:    str = "EUR"
    min_stay:    int = 1
    active:      bool = True


class TravelOSBooking(BaseModel):
    id:               str
    pms_booking_id:   str
    unit_id:          str
    check_in:         str
    check_out:        str
    guests:           int
    guest_name:       str
    guest_email:      str
    guest_phone:      str | None = None
    status:           str
    channel:          str = "travelos_mcp"
    commission_pct:   float = 5.0
    notes:            str | None = None
    created_at:       datetime | None = None
