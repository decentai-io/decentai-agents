"""A mailbox's zone, as a zone Python can reckon in.

Microsoft 365 keeps a mailbox's time zone as whatever it was set with,
and that is usually a Windows name — "Arabian Standard Time", "GMT
Standard Time" — which zoneinfo does not know. The common ones are
mapped here; an IANA name passes through untouched; anything else is
said plainly, so the agent asks for a zone rather than reckoning in the
wrong one.
"""

from __future__ import annotations

from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WINDOWS_TO_IANA = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabic Standard Time": "Asia/Baghdad",
    "Egypt Standard Time": "Africa/Cairo",
    "Israel Standard Time": "Asia/Jerusalem",
    "Jordan Standard Time": "Asia/Amman",
    "Turkey Standard Time": "Europe/Istanbul",
    "Iran Standard Time": "Asia/Tehran",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GTB Standard Time": "Europe/Bucharest",
    "FLE Standard Time": "Europe/Helsinki",
    "Russian Standard Time": "Europe/Moscow",
    "South Africa Standard Time": "Africa/Johannesburg",
    "E. Africa Standard Time": "Africa/Nairobi",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Morocco Standard Time": "Africa/Casablanca",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "SA Pacific Standard Time": "America/Bogota",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Central Standard Time (Mexico)": "America/Mexico_City",
}


def resolve_zone(name: str) -> Tuple[Optional[ZoneInfo], str, str]:
    """(zone, iana_name, problem). An IANA name passes through, a known
    Windows name is mapped, and anything else comes back with no zone
    and a sentence to say to the user."""
    raw = str(name or "").strip()
    if not raw:
        return None, "", ("The calendar reports no time zone; name one "
                          "(an IANA zone such as Asia/Dubai).")
    iana = WINDOWS_TO_IANA.get(raw, raw)
    try:
        return ZoneInfo(iana), iana, ""
    except (ZoneInfoNotFoundError, ValueError):
        return None, raw, (f"'{raw}' is not a time zone this agent can reckon "
                           f"in; name an IANA zone (Asia/Dubai, Europe/London).")
