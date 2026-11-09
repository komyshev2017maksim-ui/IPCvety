"""Часовой пояс по городу и штату прокси (запасной вариант, когда IP ещё не проверен)."""

from __future__ import annotations

import re

STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# Города в той части штата, где пояс отличается от основного.
CITY_TZ = {
    ("FL", "pensacola"): "America/Chicago",
    ("FL", "panama city"): "America/Chicago",
    ("FL", "destin"): "America/Chicago",
    ("FL", "fort walton beach"): "America/Chicago",
    ("TX", "el paso"): "America/Denver",
    ("TN", "knoxville"): "America/New_York",
    ("TN", "chattanooga"): "America/New_York",
    ("TN", "johnson city"): "America/New_York",
    ("TN", "kingsport"): "America/New_York",
    ("KY", "bowling green"): "America/Chicago",
    ("KY", "paducah"): "America/Chicago",
    ("KY", "owensboro"): "America/Chicago",
    ("IN", "gary"): "America/Chicago",
    ("IN", "hammond"): "America/Chicago",
    ("IN", "evansville"): "America/Chicago",
    ("ID", "coeur d'alene"): "America/Los_Angeles",
    ("ID", "lewiston"): "America/Los_Angeles",
    ("SD", "rapid city"): "America/Denver",
    ("NE", "scottsbluff"): "America/Denver",
    ("KS", "goodland"): "America/Denver",
    ("OR", "ontario"): "America/Boise",
    ("MI", "iron mountain"): "America/Menominee",
    ("MI", "menominee"): "America/Menominee",
    ("ND", "dickinson"): "America/Denver",
}


def parse_city_state(text: str) -> tuple[str, str | None]:
    """«Miami, FL» или «Miami, Florida» -> («miami», «FL»)."""
    text = (text or "").strip()
    if not text:
        return "", None
    parts = [p.strip() for p in re.split(r"[,/]", text) if p.strip()]
    state = None
    city = parts[0] if parts else ""
    for part in reversed(parts):
        state = _state_code(part)
        if state:
            if part is parts[0] and len(parts) == 1:
                city = ""
            break
    if state is None and len(parts) == 1:
        # «Miami FL» без запятой
        match = re.match(r"^(.*\S)\s+([A-Za-z]{2})$", text)
        if match and match.group(2).upper() in STATE_TZ:
            city, state = match.group(1), match.group(2).upper()
    return city.lower(), state


def _state_code(text: str) -> str | None:
    cleaned = text.strip().rstrip(".")
    if cleaned.upper() in STATE_TZ:
        return cleaned.upper()
    return STATE_NAMES.get(cleaned.lower())


def timezone_for(city_state: str) -> str | None:
    city, state = parse_city_state(city_state)
    if not state:
        return None
    return CITY_TZ.get((state, city), STATE_TZ[state])
