"""Reference vocabulary: areas, cuisines, features, occasions and their synonyms.

Keeping the vocabulary in one place means search, recommendation, the seed generator
and the tool descriptions shown to the LLM can never drift apart.
"""
from __future__ import annotations

AREAS = [
    "Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "Marathahalli", "Jayanagar",
    "JP Nagar", "Bannerghatta Road", "MG Road", "Malleshwaram", "Hebbal", "Electronic City",
]

# Neighbourhood graph -> "near Koramangala" still finds HSR Layout.
NEARBY = {
    "Indiranagar": ["MG Road", "Koramangala", "Marathahalli"],
    "Koramangala": ["HSR Layout", "Indiranagar", "Jayanagar", "MG Road"],
    "HSR Layout": ["Koramangala", "Electronic City", "Bannerghatta Road", "Marathahalli"],
    "Whitefield": ["Marathahalli"],
    "Marathahalli": ["Whitefield", "Indiranagar", "HSR Layout"],
    "Jayanagar": ["JP Nagar", "Koramangala", "Bannerghatta Road"],
    "JP Nagar": ["Jayanagar", "Bannerghatta Road"],
    "Bannerghatta Road": ["JP Nagar", "Jayanagar", "HSR Layout"],
    "MG Road": ["Indiranagar", "Koramangala", "Malleshwaram"],
    "Malleshwaram": ["Hebbal", "MG Road"],
    "Hebbal": ["Malleshwaram"],
    "Electronic City": ["HSR Layout"],
}

# Approximate neighbourhood centres (lat, lng). Outlet pins are placed deterministically around
# these, since the demo outlets are fictional.
AREA_COORDS = {
    "Indiranagar": (12.9719, 77.6412), "Koramangala": (12.9352, 77.6245), "HSR Layout": (12.9116, 77.6474),
    "Whitefield": (12.9698, 77.7500), "Marathahalli": (12.9569, 77.7011), "Jayanagar": (12.9250, 77.5938),
    "JP Nagar": (12.9063, 77.5857), "Bannerghatta Road": (12.8880, 77.5970), "MG Road": (12.9756, 77.6066),
    "Malleshwaram": (13.0031, 77.5643), "Hebbal": (13.0358, 77.5970), "Electronic City": (12.8452, 77.6602),
}

AREA_ALIASES = {
    "hsr": "HSR Layout", "ecity": "Electronic City", "e-city": "Electronic City", "e city": "Electronic City",
    "mg road": "MG Road", "brigade road": "MG Road", "church street": "MG Road", "itpl": "Whitefield",
    "bellandur": "Marathahalli", "outer ring road": "Marathahalli", "orr": "Marathahalli",
    "jp nagar": "JP Nagar", "bg road": "Bannerghatta Road", "btm": "Koramangala",
    "malleswaram": "Malleshwaram", "indira nagar": "Indiranagar",
}

FEATURES = [
    "outdoor_seating", "rooftop", "private_dining", "live_music", "kid_friendly",
    "wheelchair_accessible", "valet_parking", "bar", "pure_veg", "wifi", "pet_friendly", "quiet_zone",
]

FEATURE_ALIASES = {
    "outdoor": "outdoor_seating", "alfresco": "outdoor_seating", "terrace": "rooftop", "view": "rooftop",
    "private": "private_dining", "private room": "private_dining", "music": "live_music",
    "live music": "live_music", "kids": "kid_friendly", "children": "kid_friendly", "family": "kid_friendly",
    "wheelchair": "wheelchair_accessible", "accessible": "wheelchair_accessible", "parking": "valet_parking",
    "valet": "valet_parking", "drinks": "bar", "alcohol": "bar", "cocktails": "bar", "beer": "bar",
    "wine": "bar", "veg": "pure_veg", "vegetarian": "pure_veg", "pure veg": "pure_veg", "wi-fi": "wifi",
    "pets": "pet_friendly", "dog": "pet_friendly", "quiet": "quiet_zone", "calm": "quiet_zone",
}

# Free-text cuisine -> canonical cuisine labels used in the data.
CUISINE_SYNONYMS = {
    "indian": ["north indian", "south indian", "mughlai", "chettinad", "coastal", "biryani"],
    "north indian": ["north indian"], "punjabi": ["north indian"], "mughlai": ["mughlai"],
    "kebab": ["mughlai"], "tandoori": ["north indian"], "south indian": ["south indian"],
    "dosa": ["south indian"], "idli": ["south indian"], "chettinad": ["chettinad"],
    "chinese": ["pan-asian", "chinese"], "asian": ["pan-asian"], "pan asian": ["pan-asian"],
    "thai": ["thai"], "japanese": ["japanese"], "sushi": ["japanese"], "ramen": ["japanese"],
    "italian": ["italian"], "pizza": ["italian"], "pasta": ["italian"], "continental": ["continental"],
    "european": ["continental"], "grill": ["grill", "barbecue"], "bbq": ["barbecue"],
    "barbecue": ["barbecue"], "steak": ["grill"], "seafood": ["seafood", "coastal"], "fish": ["seafood"],
    "coastal": ["coastal"], "mangalorean": ["coastal"], "kerala": ["coastal"], "malabar": ["coastal"],
    "goan": ["coastal"], "biryani": ["biryani"], "hyderabadi": ["hyderabadi", "biryani"],
    "healthy": ["healthy"], "salad": ["healthy"], "vegan": ["vegan"], "keto": ["healthy"],
    "mediterranean": ["mediterranean"], "lebanese": ["lebanese"], "middle eastern": ["lebanese", "mediterranean"],
    "greek": ["mediterranean"], "cafe": ["cafe"], "coffee": ["cafe"], "breakfast": ["cafe", "south indian"],
}

OCCASIONS = {
    "date": ["rooftop", "live_music", "bar", "quiet_zone"],
    "family": ["kid_friendly", "valet_parking", "wheelchair_accessible"],
    "business": ["quiet_zone", "private_dining", "wifi"],
    "celebration": ["private_dining", "live_music", "bar"],
    "friends": ["bar", "live_music", "outdoor_seating"],
    "casual": [],
}
OCCASION_ALIASES = {
    "romantic": "date", "anniversary": "date", "date night": "date", "proposal": "date",
    "kids": "family", "parents": "family", "family dinner": "family",
    "client": "business", "meeting": "business", "work": "business", "team lunch": "business",
    "team": "celebration", "birthday": "celebration", "party": "celebration", "farewell": "celebration",
    "office party": "celebration", "hangout": "friends", "gang": "friends", "catch up": "friends",
    "quick bite": "casual", "solo": "casual",
}


def normalize_area(text: str | None) -> str | None:
    if not text:
        return None
    s = text.strip().lower()
    if s in AREA_ALIASES:
        return AREA_ALIASES[s]
    for area in AREAS:
        if area.lower() == s or area.lower() in s or s in area.lower():
            return area
    for alias, area in AREA_ALIASES.items():
        if alias in s:
            return area
    return None


def normalize_features(items) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        items = [p for p in items.replace(";", ",").split(",")]
    out = []
    for item in items:
        s = str(item).strip().lower().replace("-", " ")
        key = s.replace(" ", "_")
        if key in FEATURES:
            out.append(key)
        elif s in FEATURE_ALIASES:
            out.append(FEATURE_ALIASES[s])
    return sorted(set(out))


def normalize_occasion(text: str | None) -> str | None:
    if not text:
        return None
    s = text.strip().lower()
    if s in OCCASIONS:
        return s
    for alias, occ in OCCASION_ALIASES.items():
        if alias in s:
            return occ
    for occ in OCCASIONS:
        if occ in s:
            return occ
    return None


def expand_cuisine(text: str | None) -> set[str]:
    if not text:
        return set()
    s = text.strip().lower()
    matched = [k for k in CUISINE_SYNONYMS if k in s]
    # "south indian" must not also trigger the broad "indian" expansion.
    matched = [k for k in matched if not any(k != o and k in o for o in matched)]
    terms = set()
    for key in matched:
        terms.update(CUISINE_SYNONYMS[key])
    if not terms:
        terms.add(s)
    return terms


def outlet_coords(outlet_id: str, area: str) -> tuple[float, float]:
    """Stable pseudo-location for a demo outlet: within ~1 km of its area centre."""
    import hashlib
    lat, lng = AREA_COORDS[area]
    h = hashlib.sha256(outlet_id.encode()).digest()
    return lat + (h[0] / 255 - 0.5) * 0.016, lng + (h[1] / 255 - 0.5) * 0.016


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km (haversine)."""
    from math import asin, cos, radians, sin, sqrt
    lat1, lng1, lat2, lng2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))
