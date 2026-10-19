"""Find demo-ready scenarios in the current database.   Run:  python -m goodfoods.demo_prep

The seeded bookings are generated relative to today, so which slots are full changes from day
to day. This prints real full slots (with the alternatives Gia will offer) so the
"slot full -> recover" scene in a demo video works first time.
"""
from __future__ import annotations

from datetime import timedelta

from .config import now
from .seed import build_database
from .services import ReservationService, ToolError


def main() -> None:
    service = ReservationService(build_database())
    today = now().date()
    print(f"Today: {today:%A %d %b %Y}\n")
    print("Full slots you can use for the 'slot full' scene (8 PM, busiest days first):")
    found = 0
    days = sorted((today + timedelta(days=i) for i in range(1, 7)), key=lambda d: d.weekday() < 4)
    for d in days:
        for party in (6, 4):
            for r in service._all_restaurants():
                try:
                    out = service.check_availability(r["id"], d.isoformat(), "20:00", party)
                except ToolError:
                    continue
                if out["available"] or not out.get("alternative_times_same_day"):
                    continue
                alt = ", ".join(out["alternative_times_same_day"][:3])
                near = ", ".join(o["name"].replace("GoodFoods ", "") for o in out.get("similar_outlets_free_at_this_time", []))
                print(f"  {d:%A %d %b} · {party} guests · {r['name'].replace('GoodFoods ', '')} ({r['id']})")
                print(f"     Gia will offer: {alt}" + (f" · or {near}" if near else ""))
                print(f'     Type: "Is {r["name"].replace("GoodFoods ", "").replace(" – ", " ")} free on '
                      f'{d:%A} at 8 pm for {party}?"\n')
                found += 1
                if found >= 3:
                    return
    if not found:
        print("  None found — rebuild the demo data with:  python -m goodfoods.seed --reset")


if __name__ == "__main__":
    main()
