"""Town-name normalisation shared by the import commands and by request-time lookups.

One function has to run on gazetteer names, fuel-CSV cities and user input alike;
otherwise keys stop matching ("St. Louis" vs "Saint Louis", "Fort Worth" vs "Ft Worth").
"""

import re

_RULES = [
    (re.compile(pattern), replacement)
    for pattern, replacement in [
        (r"\bSAINTE\b", "STE"),
        (r"\bSAINT\b", "ST"),
        (r"\bFORT\b", "FT"),
        (r"\bMOUNT\b", "MT"),
        (r"\bMOUNTAIN\b", "MTN"),
    ]
]
_PUNCTUATION = re.compile(r"[.']")
_SEPARATORS = re.compile(r"[-\s]+")

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip

CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}  # fmt: skip


def normalize_town(name: str) -> str:
    """Upper-case, drop dots and apostrophes, abbreviate Saint/Fort/Mount, unify separators.

    "St. Louis" -> "ST LOUIS", "Saint-Liboire" -> "ST LIBOIRE", " Tomah " -> "TOMAH".
    """
    s = name.upper().strip()
    s = _PUNCTUATION.sub("", s)
    for pattern, replacement in _RULES:
        s = pattern.sub(replacement, s)
    return _SEPARATORS.sub(" ", s).strip()
