from __future__ import annotations
from datetime import datetime, date
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import uuid


class BookingEventType(str, Enum):
    CONFIRMED  = "booking.confirmed"
    MODIFIED   = "booking.modified"
    CANCELLED  = "booking.cancelled"
    NO_SHOW    = "booking.noshow"
    CHECKIN    = "guest.checkin"
    CHECKOUT   = "guest.checkout"


class GuestProfile(BaseModel):
    id: str
    name: str
    email: str | None = None
    phone: str | None = None
    nationality: str | None = None
    language: str = "en"
    notes: str | None = None


class Reservation(BaseModel):
    id: str
    property_id: str
    source: str
    status: str
    checkin: date
    checkout: date
    guests_adult: int
    guests_child: int = 0
    guest: GuestProfile
    total_amount: float
    currency: str = "EUR"
    notes: str | None = None


class WebHotelierWebhookPayload(BaseModel):
    event: str
    reservation_id: str
    property_id: str | None = None
    timestamp: datetime | None = None
    data: dict[str, Any] = {}


class AuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    agent: str
    action: str
    detail: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    success: bool = True
