"""Deterministic synthetic data: 75 GoodFoods outlets across 12 Bengaluru areas,
plus a realistic week of existing bookings so availability, waitlisting and
alternative-time logic are exercised from the first demo.

Run directly to rebuild:  python -m goodfoods.seed --reset
"""
from __future__ import annotations

import argparse
import random
from datetime import timedelta

from . import catalog
from .config import DB_PATH, now
from .db import Database

# (concept, cuisines, price range per person, base features, signature dishes, veg_only, hours, rating centre)
CONCEPTS = [
    ("Tandoor House", ["North Indian", "Mughlai"], (600, 1100), ["kid_friendly", "valet_parking"],
     ["butter chicken", "galouti kebab", "dal makhani"], False, ("12:00", "23:30"), 4.3),
    ("Dosa Darbar", ["South Indian"], (250, 450), ["pure_veg", "kid_friendly"],
     ["ghee roast dosa", "filter coffee", "idli vada"], True, ("07:30", "22:30"), 4.4),
    ("Coastal Kitchen", ["Coastal", "Seafood"], (800, 1400), ["bar"],
     ["chicken ghee roast", "neer dosa", "fish thali"], False, ("12:00", "23:30"), 4.4),
    ("Wok Street", ["Pan-Asian", "Chinese", "Thai"], (700, 1200), ["bar", "outdoor_seating"],
     ["dim sum", "pad thai", "hakka noodles"], False, ("12:00", "23:30"), 4.1),
    ("Trattoria", ["Italian"], (900, 1600), ["bar", "quiet_zone", "wifi"],
     ["wood-fired pizza", "truffle pasta", "tiramisu"], False, ("12:00", "23:30"), 4.4),
    ("Smokehouse", ["Barbecue", "Continental"], (1000, 1800), ["bar", "live_music", "outdoor_seating"],
     ["pulled pork", "bbq platter", "craft beer"], False, ("12:00", "00:00"), 4.2),
    ("Biryani Co.", ["Biryani", "Hyderabadi"], (400, 750), ["kid_friendly"],
     ["dum biryani", "haleem", "double ka meetha"], False, ("11:30", "23:30"), 4.3),
    ("Green Bowl", ["Healthy", "Vegan"], (500, 850), ["pure_veg", "wifi", "pet_friendly"],
     ["buddha bowl", "smoothie bowl", "millet salad"], True, ("08:00", "22:30"), 4.2),
    ("Sakura", ["Japanese"], (1400, 2400), ["quiet_zone", "bar"],
     ["sushi platter", "tonkotsu ramen", "omakase"], False, ("12:30", "23:00"), 4.6),
    ("Mezze Terrace", ["Mediterranean", "Lebanese"], (900, 1500), ["rooftop", "outdoor_seating", "bar", "live_music"],
     ["hummus trio", "shawarma", "baklava"], False, ("12:00", "00:00"), 4.4),
    ("Chettinad Mess", ["Chettinad", "South Indian"], (450, 800), ["kid_friendly"],
     ["pepper chicken", "kothu parotta", "mutton chukka"], False, ("11:30", "23:00"), 4.3),
    ("Skyline Grill", ["Continental", "Grill"], (1600, 2800),
     ["rooftop", "bar", "live_music", "valet_parking", "private_dining"],
     ["tenderloin steak", "grilled prawns", "signature cocktails"], False, ("17:00", "00:00"), 4.6),
    ("Café Brew", ["Cafe", "Continental"], (300, 600), ["wifi", "pet_friendly", "outdoor_seating"],
     ["cold brew", "eggs benedict", "banana bread"], False, ("08:00", "23:00"), 4.2),
]

STREETS = ["100 Feet Road", "80 Feet Road", "Main Road", "1st Cross", "5th Block", "Service Road",
           "Ring Road", "High Street", "Cross Road", "Market Road"]


def generate_restaurants(n: int = 75, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    combos = [(c, a) for c in CONCEPTS for a in catalog.AREAS]
    rng.shuffle(combos)
    # Guarantee every area and every concept is represented before filling the rest.
    chosen, seen_area, seen_concept = [], set(), set()
    for c, a in combos:
        if a not in seen_area or c[0] not in seen_concept:
            chosen.append((c, a)); seen_area.add(a); seen_concept.add(c[0])
    for combo in combos:
        if len(chosen) >= n:
            break
        if combo not in chosen:
            chosen.append(combo)
    chosen = sorted(chosen[:n], key=lambda x: (catalog.AREAS.index(x[1]), x[0][0]))

    out = []
    for i, (c, area) in enumerate(chosen, start=1):
        concept, cuisines, (lo, hi), base_feats, dishes, veg, (open_t, close_t), centre = c
        feats = set(base_feats)
        for feat, p in (("wheelchair_accessible", 0.45), ("valet_parking", 0.3), ("private_dining", 0.3),
                        ("outdoor_seating", 0.15), ("live_music", 0.1)):
            if rng.random() < p:
                feats.add(feat)
        if veg:
            feats.discard("bar")
        size = rng.choice(["small", "medium", "large"])
        scale = {"small": 1, "medium": 2, "large": 3}[size]
        tables = {"2": rng.randint(3, 6) * scale, "4": rng.randint(4, 8) * scale,
                  "6": rng.randint(1, 3) * scale, "8": rng.randint(0, 2) * max(1, scale - 1)}
        if tables["8"] == 0 and rng.random() < 0.5:
            tables["8"] = 1
        seats = sum(int(k) * v for k, v in tables.items())
        pdr = rng.choice([12, 16, 20, 24]) if "private_dining" in feats else 0
        rating = round(min(4.9, max(3.6, rng.gauss(centre, 0.18))), 1)
        price = int(round(rng.randint(lo, hi), -1))
        close = "23:59" if close_t == "00:00" else close_t
        out.append({
            "id": f"BLR-{i:03d}",
            "name": f"GoodFoods {concept} – {area}",
            "concept": concept,
            "area": area,
            "address": f"{rng.randint(1, 450)}, {rng.choice(STREETS)}, {area}, Bengaluru",
            "phone": f"+91-80-4{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
            "cuisines": cuisines,
            "price_per_person": price,
            "rating": rating,
            "review_count": rng.randint(180, 4200),
            "open_time": open_t,
            "close_time": close,
            "tables": tables,
            "total_seats": seats,
            "private_dining_capacity": pdr,
            "features": sorted(feats),
            "signature_dishes": dishes,
            "description": f"{size.capitalize()} {concept} outlet in {area} serving {', '.join(cuisines)}"
                           f" ({seats} seats).",
            "veg_only": veg,
        })
    return out


def seed_reservations(service, days: int = 7, seed: int = 7) -> int:
    """Fill the next `days` with bookings; popular outlets and peak slots fill up first."""
    rng = random.Random(seed)
    today = now().date()
    first_names = ["Aarav", "Diya", "Kabir", "Ananya", "Rohan", "Isha", "Vikram", "Meera", "Arjun", "Sneha",
                   "Nikhil", "Pooja", "Rahul", "Kavya", "Siddharth", "Priya", "Aditya", "Neha"]
    peak = ["19:30", "20:00", "20:30", "21:00"]
    other = ["12:30", "13:00", "13:30", "19:00", "21:30"]
    created = 0
    restaurants = service.db.query("SELECT * FROM restaurants")
    for offset in range(1, days + 1):
        d = today + timedelta(days=offset)
        weekend = d.weekday() >= 4
        for r in restaurants:
            demand = (r["rating"] - 3.5) * (1.6 if weekend else 1.0)
            for _ in range(int(demand * rng.randint(6, 14))):
                t = rng.choice(peak) if rng.random() < 0.7 else rng.choice(other)
                party = rng.choices([2, 3, 4, 5, 6, 8], weights=[35, 15, 25, 10, 10, 5])[0]
                phone = f"9{rng.randint(100000000, 999999999)}"
                try:
                    service.make_reservation(r["id"], d.isoformat(), t, party, rng.choice(first_names),
                                             phone, source="seed")
                    created += 1
                except Exception:
                    pass   # outlet full / outside hours: exactly what we want to simulate
    return created


def build_database(path=DB_PATH, reset: bool = False, with_bookings: bool = True) -> Database:
    from .services import ReservationService
    from pathlib import Path

    if reset and str(path) != ":memory:" and Path(path).exists():
        Path(path).unlink()
    db = Database(path)
    if db.restaurant_count() == 0:
        for r in generate_restaurants():
            db.insert_restaurant(r)
        if with_bookings:
            seed_reservations(ReservationService(db))
    return db


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the GoodFoods demo database")
    ap.add_argument("--reset", action="store_true", help="delete and rebuild")
    args = ap.parse_args()
    database = build_database(reset=args.reset)
    n_res = database.query("SELECT COUNT(*) AS n FROM reservations")[0]["n"]
    print(f"{database.restaurant_count()} restaurants, {n_res} seeded reservations -> {DB_PATH}")
