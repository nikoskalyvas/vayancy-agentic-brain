"""
TravelOS Adapter Base — every PMS adapter must implement this interface.
The hospitality MCP facade only talks to this contract, never to a specific PMS.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class AvailabilityResult:
    available: bool
    units: list[dict]          # [{unit_id, name, max_guests, amenities, ...}]
    currency: str = "EUR"
    error: str | None = None


@dataclass
class RateDetails:
    unit_id: str
    check_in: str
    check_out: str
    nights: int
    base_rate: float           # per night
    total_price: float
    cleaning_fee: float
    tax_amount: float
    gross_total: float
    currency: str
    cancellation_policy: str
    min_stay: int
    error: str | None = None


@dataclass
class BookingResult:
    success: bool
    pms_booking_id: str | None  # ID in the PMS (WebHotelier reservation_id etc.)
    status: str                 # confirmed | failed | pending
    error: str | None = None
    raw: dict | None = None


@dataclass
class BookingDetails:
    pms_booking_id: str
    status: str
    unit_id: str
    unit_name: str
    check_in: str
    check_out: str
    guest_name: str
    guest_email: str | None
    guest_phone: str | None
    total_amount: float
    currency: str
    notes: str | None = None
    error: str | None = None


class PMSAdapter(ABC):
    """
    Every PMS adapter must implement these methods.
    All methods are async and return typed dataclasses.
    Adapters handle their own auth, retries, and error normalization.
    """

    @abstractmethod
    async def check_availability(
        self,
        check_in: str,
        check_out: str,
        guests: int,
        unit_id: str | None = None,
    ) -> AvailabilityResult:
        """Return available units for dates. Optionally filter to specific unit."""
        ...

    @abstractmethod
    async def get_rate_details(
        self,
        unit_id: str,
        check_in: str,
        check_out: str,
        guests: int,
    ) -> RateDetails:
        """Return full pricing breakdown for a specific unit and dates."""
        ...

    @abstractmethod
    async def create_booking(
        self,
        unit_id: str,
        check_in: str,
        check_out: str,
        guests: int,
        guest_name: str,
        guest_email: str,
        guest_phone: str,
        idempotency_key: str,
        notes: str = "",
        hold_ref: str | None = None,  # pass hold_id if a hold was created
    ) -> BookingResult:
        """Create a confirmed booking in the PMS. Must be idempotent."""
        ...

    @abstractmethod
    async def get_booking_details(self, pms_booking_id: str) -> BookingDetails:
        """Fetch full booking details from PMS."""
        ...

    @abstractmethod
    async def cancel_booking(
        self, pms_booking_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Cancel a booking in the PMS. Returns status dict."""
        ...
