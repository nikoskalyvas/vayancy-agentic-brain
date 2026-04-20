from .base import PMSAdapter, AvailabilityResult, RateDetails, BookingResult, BookingDetails
from .webhotelier import WebHotelierAdapter
from .hosthub import HostHubAdapter

__all__ = [
    "PMSAdapter",
    "AvailabilityResult",
    "RateDetails",
    "BookingResult",
    "BookingDetails",
    "WebHotelierAdapter",
    "HostHubAdapter",
]
