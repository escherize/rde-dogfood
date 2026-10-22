#!/usr/bin/env python3
"""Generate the raw landing data for the shop_warehouse dataset as CSV files under data/.

A Shopify DTC outdoor-apparel store ("Northbound Supply Co.") over two years
(2024-10-01 .. 2026-09-30, shop timezone America/New_York), landed the way an ingestion
tool would land it: raw_shopify, raw_stripe and raw_marketing. The transformed schemas are
built afterwards by SQL inside Postgres (sql/), never here.

Everything derives from one seeded PRNG, so reruns are byte-identical. Python stdlib only.
Also writes data/expected.json: totals computed from the in-memory rows, which the SQL
validation compares against the marts so the transforms are checked against an
independent oracle.

Usage: python3 generate.py
"""

import bisect
import csv
import json
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20241001
HERE = Path(__file__).resolve().parent
OUT = HERE / "data"

SHOP_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
START = date(2024, 10, 1)
END = date(2026, 9, 30)
# The connector's last sync: nothing that happens after this exists in the landing tables.
CUTOFF = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
TARGET_ORDERS = 120_000
NEVER_ORDERED_ACCOUNTS = 1_400
NON_CONVERTING_SESSIONS = 750_000
SHOP_DOMAIN = "northboundsupply.com"

rng = random.Random(SEED)


# --------------------------------------------------------------------------- helpers


def rid() -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def money(cents: int | None) -> str | None:
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def ts_local(dt: datetime) -> str:
    """Shopify API style: ISO-8601 with the shop's UTC offset."""
    return dt.astimezone(SHOP_TZ).isoformat(timespec="seconds")


def ts_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def jdump(obj) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, separators=(",", ":"))


def boolstr(b: bool | None) -> str | None:
    if b is None:
        return None
    return "true" if b else "false"


def pick(weighted: list[tuple[object, float]]):
    items, weights = zip(*weighted)
    return rng.choices(items, weights=weights, k=1)[0]


def local_dt(d: date, seconds: int) -> datetime:
    naive = datetime(d.year, d.month, d.day) + timedelta(seconds=seconds)
    return naive.replace(tzinfo=SHOP_TZ)


def thanksgiving(year: int) -> date:
    nov1 = date(year, 11, 1)
    first_thu = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    return first_thu + timedelta(weeks=3)


def mothers_day(year: int) -> date:
    may1 = date(year, 5, 1)
    first_sun = may1 + timedelta(days=(6 - may1.weekday()) % 7)
    return first_sun + timedelta(weeks=1)


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


ALL_DAYS = list(days(START, END))
MONTH_INDEX = {}
for _d in ALL_DAYS:
    MONTH_INDEX.setdefault((_d.year, _d.month), len(MONTH_INDEX))


def month_idx(d: date) -> int:
    return MONTH_INDEX[(d.year, d.month)]


# --------------------------------------------------------------------------- calendar

WEEKDAY_MULT = [1.06, 1.0, 0.97, 0.97, 0.93, 1.02, 1.14]  # Mon..Sun
MONTH_MULT = [0.78, 0.82, 0.93, 0.98, 1.04, 0.95, 1.0, 0.94, 0.95, 1.0, 1.12, 1.3]


def promo_mult(d: date) -> float:
    tg = thanksgiving(d.year)
    bf = tg + timedelta(days=1)
    cm = tg + timedelta(days=4)
    spikes = {tg: 2.2, bf: 5.4, bf + timedelta(days=1): 3.6, bf + timedelta(days=2): 3.0, cm: 3.9,
              cm + timedelta(days=1): 1.8}
    if d in spikes:
        return spikes[d]
    if tg - timedelta(days=10) <= d < tg:
        return 1.45  # early-access BFCM
    if d.month == 12 and d.day <= 18:
        return 1.25  # holiday gifting until the shipping cutoff
    if d.month == 12 and d.day >= 22:
        return 0.62
    md = mothers_day(d.year)
    if md - timedelta(days=9) <= d <= md - timedelta(days=2):
        return 1.3
    if d.month == 7 and 15 <= d.day <= 21:
        return 1.55  # mid-summer sale
    return 1.0


def is_bfcm(d: date) -> bool:
    tg = thanksgiving(d.year)
    return tg - timedelta(days=10) <= d <= tg + timedelta(days=5)


def is_summer_sale(d: date) -> bool:
    return d.month == 7 and 15 <= d.day <= 21


def daily_mu(d: date) -> float:
    t = (d - START).days
    growth = 1.0 + 0.85 * t / 365.0  # ~85% YoY growth on the base
    return growth * WEEKDAY_MULT[d.weekday()] * MONTH_MULT[d.month - 1] * promo_mult(d)


_mus = [daily_mu(d) for d in ALL_DAYS]
_scale = TARGET_ORDERS / sum(_mus)
DAILY_ORDERS = {d: max(1, round(rng.gauss(mu * _scale, math.sqrt(mu * _scale)))) for d, mu in zip(ALL_DAYS, _mus)}

# Hour-of-day profile (shop local); BFCM days lean to midnight drops.
HOUR_W = [1.2, 0.7, 0.4, 0.3, 0.3, 0.4, 0.8, 1.4, 2.0, 2.6, 3.0, 3.2,
          3.4, 3.3, 3.2, 3.1, 3.1, 3.3, 3.6, 4.1, 4.4, 4.2, 3.4, 2.2]
HOUR_W_BF = [4.0, 3.0, 1.5] + HOUR_W[3:]


def order_time(d: date) -> datetime:
    w = HOUR_W_BF if d == thanksgiving(d.year) + timedelta(days=1) else HOUR_W
    hour = rng.choices(range(24), weights=w, k=1)[0]
    return local_dt(d, hour * 3600 + rng.randrange(3600))


# --------------------------------------------------------------------------- reference data

FIRST = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
         "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Chris", "Karen",
         "Daniel", "Lisa", "Matthew", "Nancy", "Anthony", "Betty", "Mark", "Sandra", "Steven", "Ashley",
         "Andrew", "Emily", "Joshua", "Michelle", "Kevin", "Amanda", "Brian", "Melissa", "Ryan", "Laura",
         "Jacob", "Rebecca", "Noah", "Hannah", "Liam", "Olivia", "Ethan", "Emma", "Lucas", "Ava", "Mia",
         "Oliver", "Sofia", "Aiden", "Chloe", "Mateo", "Priya", "Wei", "Yuki", "Aaliyah", "Diego", "Fatima",
         "Hiroshi", "Ingrid", "Jonas", "Kai", "Leila", "Marco", "Nadia", "Omar", "Rosa", "Sven", "Tariq",
         "Uma", "Zoe", "Camille", "Lea", "Felix", "Charlotte", "Harper", "Jack", "Isla", "Finn", "Maya"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
        "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Patel",
        "Kim", "Chen", "Singh", "Muller", "Schmidt", "Dubois", "Moreau", "O'Brien", "MacDonald", "Tanaka",
        "Larsen", "Novak", "Rossi", "Kowalski", "Haddad", "Okafor", "Silva", "Costa", "Fischer", "Becker"]
DOMAINS = [("gmail.com", 46), ("yahoo.com", 9), ("outlook.com", 7), ("icloud.com", 12), ("hotmail.com", 6),
           ("proton.me", 2), ("aol.com", 2), ("me.com", 2), ("comcast.net", 2), ("gmx.de", 1),
           ("hotmail.co.uk", 2), ("bigpond.com", 1), ("orange.fr", 1)]

# country -> (weight, presentment currency, regions with tax rate in basis points)
COUNTRIES = {
    "US": (72, "USD", [("CA", 825, 12), ("NY", 800, 7), ("TX", 625, 8), ("WA", 650, 5), ("CO", 290, 6),
                       ("OR", 0, 5), ("IL", 625, 4), ("MA", 625, 4), ("FL", 600, 6), ("UT", 610, 3),
                       ("MN", 688, 3), ("NC", 475, 3), ("GA", 400, 3), ("PA", 600, 4), ("MT", 0, 2),
                       ("VT", 600, 2), ("AZ", 560, 3), ("MI", 600, 3), ("NJ", 663, 3), ("VA", 530, 3)]),
    "CA": (10, "CAD", [("ON", 1300, 40), ("BC", 1200, 25), ("QC", 1498, 15), ("AB", 500, 15), ("NS", 1500, 5)]),
    "GB": (7, "GBP", [("ENG", 2000, 85), ("SCT", 2000, 10), ("WLS", 2000, 5)]),
    "AU": (5, "AUD", [("NSW", 1000, 40), ("VIC", 1000, 35), ("QLD", 1000, 25)]),
    "DE": (3, "EUR", [("BE", 1900, 30), ("BY", 1900, 40), ("HH", 1900, 30)]),
    "FR": (3, "EUR", [("IDF", 2000, 60), ("ARA", 2000, 40)]),
}
FX = {"USD": 1.0, "CAD": 1.36, "GBP": 0.79, "AUD": 1.52, "EUR": 0.92}
CITIES = {"US": ["Denver", "Portland", "Seattle", "Austin", "Brooklyn", "Boulder", "Chicago", "Boston",
                 "Oakland", "Minneapolis", "Asheville", "Salt Lake City", "Bozeman", "Burlington", "Atlanta"],
          "CA": ["Toronto", "Vancouver", "Montreal", "Calgary", "Halifax", "Ottawa"],
          "GB": ["London", "Manchester", "Edinburgh", "Bristol", "Cardiff", "Leeds"],
          "AU": ["Sydney", "Melbourne", "Brisbane"],
          "DE": ["Berlin", "Munich", "Hamburg"],
          "FR": ["Paris", "Lyon", "Grenoble"]}
STREETS = ["Main St", "Oak Ave", "Pine St", "Maple Dr", "Cedar Ln", "Elm St", "Lake Rd", "Hill St", "Park Ave",
           "River Rd", "High St", "Church Rd", "Station Rd", "Mill Ln", "Ridge Rd"]

CATEGORIES = {
    # category: (product count, price range dollars, variant style, monthly seasonality Jan..Dec)
    "Tops": (120, (28, 78), "apparel", [0.8, 0.8, 1.0, 1.1, 1.2, 1.3, 1.3, 1.2, 1.0, 0.9, 0.9, 1.0]),
    "Bottoms": (70, (48, 118), "apparel", [1.0] * 12),
    "Outerwear": (55, (98, 348), "apparel", [1.3, 1.0, 0.8, 0.6, 0.4, 0.3, 0.3, 0.4, 0.7, 1.2, 1.6, 1.7]),
    "Footwear": (45, (89, 219), "shoes", [0.9, 0.9, 1.1, 1.2, 1.1, 1.0, 1.0, 1.0, 1.1, 1.0, 1.0, 1.0]),
    "Bags": (45, (39, 249), "color", [0.9, 0.9, 1.0, 1.0, 1.0, 1.1, 1.1, 1.2, 1.1, 0.9, 1.1, 1.3]),
    "Accessories": (102, (12, 58), "color", [0.9, 0.8, 0.9, 0.9, 1.0, 1.0, 1.0, 0.9, 0.9, 1.0, 1.3, 1.6]),
    "Camp & Home": (60, (18, 189), "color", [0.6, 0.6, 0.8, 1.1, 1.4, 1.6, 1.6, 1.3, 1.0, 0.8, 1.0, 1.3]),
    "Gift Cards": (3, (25, 100), "none", [0.5, 0.4, 0.4, 0.5, 0.6, 0.5, 0.5, 0.5, 0.5, 0.6, 1.2, 3.5]),
}
ADJ = ["Ridgeline", "Alpine", "Summit", "Cascade", "Timberline", "Canyon", "Basecamp", "Trailhead", "Glacier",
       "Highland", "Driftwood", "Granite", "Juniper", "Sierra", "Tundra", "Coastal", "Prairie", "Boreal",
       "Switchback", "Headwall", "Meridian", "Sawtooth", "Harbor", "Redwood", "Aspen"]
NOUNS = {
    "Tops": ["Merino Tee", "Long Sleeve Henley", "Flannel Shirt", "Sun Hoodie", "Pocket Tee", "Crewneck Sweatshirt",
             "Quarter-Zip", "Tank", "Polo", "Waffle Knit Top", "Chambray Shirt", "Fleece Pullover"],
    "Bottoms": ["Trail Pant", "Climbing Short", "Canvas Work Pant", "Jogger", "5-Pocket Jean", "Hiking Short",
                "Fleece Pant", "Convertible Pant", "Chino"],
    "Outerwear": ["Rain Shell", "Down Jacket", "Insulated Parka", "Softshell Jacket", "Fleece Vest", "Wind Shell",
                  "Puffer Vest", "Chore Coat", "Ski Shell"],
    "Footwear": ["Trail Runner", "Hiking Boot", "Camp Moc", "Approach Shoe", "Winter Boot", "Sandal", "Sneaker"],
    "Bags": ["Daypack 22L", "Duffel 40L", "Sling Bag", "Tote", "Hip Pack", "Travel Pack 35L", "Dopp Kit",
             "Rolltop Backpack"],
    "Accessories": ["Beanie", "Trucker Hat", "Wool Socks", "Neck Gaiter", "Gloves", "Belt", "Bandana",
                    "Water Bottle", "Sunglasses", "Dad Cap", "Keychain", "Patch Set"],
    "Camp & Home": ["Enamel Mug", "Camp Blanket", "Hammock", "Camp Chair", "Lantern", "Pour-Over Set",
                    "Throw Pillow", "Wool Blanket", "Cutting Board", "Candle"],
    "Gift Cards": ["Gift Card"],
}
COLORS = ["Black", "Navy", "Olive", "Rust", "Heather Grey", "Sand", "Forest", "Slate", "Ochre", "Cream", "Teal"]
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
SHOE_SIZES = ["7", "8", "9", "10", "11", "12", "13"]


# --------------------------------------------------------------------------- products

products: list[dict] = []
variants: list[dict] = []
_pid = 8_000_000_000_000
_vid = 44_000_000_000_000
_used_titles: set[str] = set()

for cat, (count, (lo, hi), style, _season) in CATEGORIES.items():
    for i in range(count):
        if cat == "Gift Cards":
            title = ["Northbound Gift Card", "Northbound E-Gift Card", "Holiday Gift Card"][i]
        else:
            while True:
                title = f"{rng.choice(ADJ)} {rng.choice(NOUNS[cat])}"
                if title not in _used_titles:
                    break
                title = f"{title} {rng.choice(['II', '2.0', 'Lite', 'Pro', 'Classic', 'Heavyweight'])}"
                if title not in _used_titles:
                    break
        _used_titles.add(title)
        _pid += rng.randint(1_000, 90_000)
        # 68% of the catalog predates the data window; the rest launches in seasonal drops.
        if rng.random() < 0.68 or cat == "Gift Cards":
            created = local_dt(START - timedelta(days=rng.randint(30, 900)), rng.randrange(9 * 3600, 18 * 3600))
        else:
            created = local_dt(START + timedelta(days=rng.randint(0, 690)), rng.randrange(9 * 3600, 18 * 3600))
        status, archived_at = "active", None
        roll = rng.random()
        if roll < 0.09 and cat != "Gift Cards":
            archived_at = created + timedelta(days=rng.randint(120, 700))
            if archived_at.date() > END:
                archived_at = None
            else:
                status = "archived"
        elif roll < 0.12 and cat != "Gift Cards":
            status = "draft"  # never published, never sold
        price = rng.randint(lo, hi) * 100 - (5 if rng.random() < 0.3 else 0)
        if cat == "Gift Cards":
            price = None
        p = {
            "id": _pid, "title": title, "category": cat, "style": style, "status": status,
            "created_at": created, "archived_at": archived_at, "price": price,
            "vendor": "Northbound Supply Co." if rng.random() < 0.88 else rng.choice(
                ["Northbound x Field Notes", "Northbound x Topo Collective", "Wildway Goods", "Kettle & Pine"]),
            "handle": title.lower().replace(" ", "-").replace(".", "").replace("&", "and").replace("'", ""),
            "weight": rng.random() ** 2.2,  # long-tailed popularity
            "deleted": False, "missing_from_raw": False, "variants": [],
        }
        if status == "archived" and rng.random() < 0.25:
            p["deleted"] = True  # removed from the store; the connector soft-deletes it
        products.append(p)

# Four products deleted from Shopify before the connector's first sync: their line items still
# point at them, but no product row ever landed (realistic raw-layer orphans).
for i in range(4):
    _pid += rng.randint(1_000, 90_000)
    title = f"{rng.choice(ADJ)} {rng.choice(NOUNS['Tops'])} (Launch Edition)"
    products.append({
        "id": _pid, "title": title, "category": "Tops", "style": "apparel", "status": "archived",
        "created_at": local_dt(START - timedelta(days=400), 36_000), "archived_at": local_dt(date(2024, 12, 1), 0),
        "price": 4_400, "vendor": "Northbound Supply Co.", "handle": title.lower().replace(" ", "-"),
        "weight": 0.9, "deleted": False, "missing_from_raw": True, "variants": [],
    })

for p in products:
    style = p["style"]
    if style == "apparel":
        colors = rng.sample(COLORS, rng.randint(1, 3))
        sizes = SIZES[: rng.choice([5, 5, 6])][rng.choice([0, 1]):]
        opts = [(c, s) for c in colors for s in sizes]
    elif style == "shoes":
        colors = rng.sample(COLORS, rng.randint(1, 2))
        opts = [(c, s) for c in colors for s in SHOE_SIZES]
    elif style == "color":
        opts = [(c, None) for c in rng.sample(COLORS, rng.randint(1, 4))]
    else:
        opts = [(None, amt) for amt in ("25", "50", "100", "250")]
    for color, size in opts:
        _vid += rng.randint(100, 9_000)
        if style == "none":
            price = int(size) * 100
            vtitle = f"${size}"
        else:
            price = p["price"]
            vtitle = " / ".join(x for x in (color, size) if x)
        compare_at = None
        if style != "none" and rng.random() < 0.12:
            compare_at = price
            price = int(round(price * 0.75 / 100.0)) * 100 - 5
        sku = None if style == "none" else (
            f"NB-{p['category'][:3].upper()}-{p['id'] % 100000:05d}-"
            f"{(color or 'NA')[:3].upper()}{('-' + size) if size else ''}")
        v = {
            "id": _vid, "product_id": p["id"], "title": vtitle, "sku": sku, "price": price,
            "compare_at": compare_at, "color": color, "size": size,
            "weight_grams": None if style == "none" else rng.randint(80, 2400),
            "inventory": rng.randint(-3, 400), "created_at": p["created_at"],
        }
        variants.append(v)
        p["variants"].append(v)

SIZE_W = {"XS": 4, "S": 14, "M": 30, "L": 28, "XL": 16, "XXL": 6, "7": 8, "8": 14, "9": 20, "10": 22, "11": 18,
          "12": 12, "13": 6}


def pick_variant(p: dict) -> dict:
    vs = p["variants"]
    return rng.choices(vs, weights=[SIZE_W.get(v["size"], 10) for v in vs], k=1)[0]


# Per-month cumulative weights over the products on sale that month.
PRODUCTS_BY_MONTH: list[tuple[list[dict], list[float]]] = []
for (y, m), _ in sorted(MONTH_INDEX.items(), key=lambda kv: kv[1]):
    m_start = local_dt(date(y, m, 1), 0)
    m_end = local_dt(date(y + (m == 12), m % 12 + 1, 1), 0)
    avail, cum, total = [], [], 0.0
    for p in products:
        if p["status"] == "draft" or p["created_at"] >= m_end:
            continue
        if p["archived_at"] is not None and p["archived_at"] <= m_start:
            continue
        w = p["weight"] * CATEGORIES[p["category"]][3][m - 1]
        if p["created_at"] >= m_start - timedelta(days=60):
            w *= 1.8  # launch buzz
        total += w
        avail.append(p)
        cum.append(total)
    PRODUCTS_BY_MONTH.append((avail, cum))


def pick_product(d: date, at: datetime) -> dict:
    avail, cum = PRODUCTS_BY_MONTH[month_idx(d)]
    while True:
        p = avail[bisect.bisect_left(cum, rng.random() * cum[-1])]
        if p["created_at"] <= at and (p["archived_at"] is None or p["archived_at"] > at):
            return p


# --------------------------------------------------------------------------- discount codes

discounts: list[dict] = []
_did = 1_200_000_000_000


def add_discount(code, title, value_type, value, starts, ends, kind):
    global _did
    _did += rng.randint(10_000, 500_000)
    d = {"id": _did, "code": code, "title": title, "value_type": value_type, "value": value,
         "starts_at": starts, "ends_at": ends, "kind": kind, "usage_count": 0,
         "created_at": starts - timedelta(days=rng.randint(1, 14))}
    discounts.append(d)
    return d


WELCOME = add_discount("WELCOME10", "Welcome 10% off first order", "percentage", 10,
                       local_dt(date(2023, 3, 1), 0), None, "welcome")
BFCM = {y: add_discount(f"BFCM{y}", f"Black Friday Cyber Monday {y}", "percentage", 25,
                        local_dt(thanksgiving(y) - timedelta(days=10), 0),
                        local_dt(thanksgiving(y) + timedelta(days=6), 0), "bfcm") for y in (2024, 2025)}
SUMMER = {y: add_discount(f"SUMMER{y % 100}", f"Summer Sale {y}", "percentage", 15,
                          local_dt(date(y, 7, 15), 0), local_dt(date(y, 7, 22), 0), "summer") for y in (2025, 2026)}
VIP_CODE = add_discount("VIPCREW20", "VIP crew 20%", "percentage", 20, local_dt(date(2024, 6, 1), 0), None, "vip")
INFLUENCERS = []
_handles = ["TRAILJESS", "HIKEWITHMO", "VANLIFEKAI", "SUMMITSAM", "PNWPRIYA", "DIRTBAGDAN", "ALPINEANA",
            "CAMPCOOKCHRIS", "RIVERROSA", "GRAVELGREG", "BOULDERBEA", "SNOWSVEN", "COASTCLAIRE", "PEAKPETE",
            "WILDWEI", "TENTTARIQ", "MOSSYMAYA", "FIELDFINN", "LOOPLEILA", "RIDGERYAN"]
for h in _handles:
    s = START + timedelta(days=rng.randint(-120, 600))
    e = s + timedelta(days=rng.randint(60, 300))
    if rng.random() < 0.5:
        INFLUENCERS.append(add_discount(f"{h}15", f"Creator code {h.title()}", "percentage", 15,
                                        local_dt(s, 0), local_dt(e, 0), "creator"))
    else:
        INFLUENCERS.append(add_discount(f"{h}10", f"Creator code {h.title()} $10", "fixed_amount", 1000,
                                        local_dt(s, 0), local_dt(e, 0), "creator"))
# Expired/never-used codes that still sit in the admin.
for code, title in [("SPRING23", "Spring 2023"), ("FREESHIPJUNE", "June free shipping test"),
                    ("STAFF40", "Staff discount"), ("SORRY15", "Customer service make-good")]:
    add_discount(code, title, "percentage", 15 if code != "STAFF40" else 40,
                 local_dt(date(2023, 4, 1), 0), local_dt(date(2023, 6, 30), 0) if "23" in code else None, "other")


def creator_code(at: datetime):
    live = [c for c in INFLUENCERS if c["starts_at"] <= at < c["ends_at"]]
    return rng.choice(live) if live else None


# --------------------------------------------------------------------------- customers

customers: list[dict] = []  # raw accounts (includes duplicates, staff, deleted)
_cid = 7_100_000_000_000
_used_emails: set[str] = set()


def new_email(first: str, last: str) -> str:
    base = rng.choice([f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}", f"{first}_{last}",
                       f"{last}.{first}", f"{first}{rng.randint(1, 99)}"])
    base = base.replace("'", "").replace(" ", "")
    domain = pick(DOMAINS)
    while True:
        email = f"{base}{rng.randint(1, 999) if rng.random() < 0.45 else ''}@{domain}"
        if email.lower() not in _used_emails:
            _used_emails.add(email.lower())
            break
        base = f"{base}{rng.randint(0, 9)}"
    # Most land lowercased; some keep the capitalisation the customer typed.
    return email if rng.random() < 0.18 else email.lower()


def casing_variant(email: str) -> str:
    local, domain = email.split("@")
    choices = [email.lower(), email.upper(), local.title() + "@" + domain.title(), " " + email.lower(),
               local.capitalize() + "@" + domain.upper(), email.lower() + " "]
    choices = [c for c in choices if c != email]
    return rng.choice(choices)


def make_customer(created: datetime, first=None, last=None, email=None, kind="real", country=None) -> dict:
    global _cid
    _cid += rng.randint(1_000, 400_000)
    first = first or rng.choice(FIRST)
    last = last or rng.choice(LAST)
    country = country or pick([(c, v[0]) for c, v in COUNTRIES.items()])
    region, tax_bp = pick([((r, t), w) for r, t, w in COUNTRIES[country][2]])
    c = {
        "id": _cid, "first_name": first, "last_name": last, "email": email or new_email(first.lower(), last.lower()),
        "phone": (f"+1{rng.randint(2000000000, 9899999999)}" if country in ("US", "CA") else
                  f"+{ {'GB': 44, 'AU': 61, 'DE': 49, 'FR': 33}[country]}{rng.randint(100000000, 999999999)}")
        if rng.random() < 0.42 else None,
        "country": country, "region": region, "tax_bp": tax_bp, "currency": COUNTRIES[country][1],
        "city": rng.choice(CITIES[country]), "zip": f"{rng.randint(10000, 99999)}" if country == "US" else
        f"{rng.choice('ABCEGHJKLMNPRSTVXY')}{rng.randint(1, 9)}{rng.choice('ABCEGHJKLMNPRSTVWXYZ')}",
        "street": f"{rng.randint(1, 9999)} {rng.choice(STREETS)}",
        "accepts_marketing": rng.random() < 0.58, "created_at": created, "kind": kind,
        "loyalty": pick([(1, 52), (2, 25), (4, 12), (8, 8), (20, 3)]),
        "twin_planned": kind == "real" and rng.random() < 0.035, "twin": None, "canonical": None,
        "redacted": kind == "real" and rng.random() < 0.003, "deleted": False,
        "orders": [], "acq_channel": None, "tags": [], "anon_id": rid(),
    }
    if c["loyalty"] >= 8:
        c["tags"].append("vip")
    if kind == "staff":
        c["tags"].append("staff")
    customers.append(c)
    return c


STAFF = [make_customer(local_dt(date(2023, 2, 1), 40_000), first=f, last=l, email=e, kind="staff", country="US")
         for f, l, e in [("Store", "Admin", f"admin@{SHOP_DOMAIN}"), ("QA", "Tester", f"qa+1@{SHOP_DOMAIN}"),
                         ("QA", "Tester", f"qa+checkout@{SHOP_DOMAIN}")]]


# --------------------------------------------------------------------------- acquisition / attribution

PAID_CHANNELS = ["google_ads", "meta_ads", "tiktok_ads", "pinterest_ads"]
ACQ_MIX = [("google_ads", 22), ("meta_ads", 21), ("tiktok_ads", 6), ("pinterest_ads", 3), ("email", 5),
           ("organic_search", 17), ("direct", 13), ("affiliate", 6), ("organic_social", 7)]
REPEAT_MIX = [("email", 38), ("direct", 24), ("google_ads", 12), ("meta_ads", 9), ("organic_search", 9),
              ("tiktok_ads", 2), ("pinterest_ads", 1), ("affiliate", 2), ("organic_social", 3)]


def channel_for(c: dict | None, d: date) -> str:
    if c is None or not c["orders"]:
        ch = pick(ACQ_MIX)
        if is_bfcm(d) and rng.random() < 0.25:
            ch = rng.choice(["meta_ads", "google_ads", "email"])
        return ch
    ch = pick(REPEAT_MIX)
    if is_bfcm(d) and rng.random() < 0.3:
        ch = "email"
    return ch


# --------------------------------------------------------------------------- orders

orders: list[dict] = []
tickets: list[dict] = []  # each account appears `loyalty` times
first_order_index: list[dict] = []  # accounts in order of first purchase
first_order_prefix: dict[date, int] = {}
guest_emails: list[str] = []


def pick_existing(at: datetime) -> dict:
    # Churned customers stop buying; retry a few draws before settling for a lapsed one.
    for _ in range(30):
        if rng.random() < 0.3 and len(tickets) > 4000:
            c = tickets[-rng.randint(1, 4000)]  # recently acquired customers come back sooner
        else:
            c = rng.choice(tickets)
        if c["active_until"] >= at:
            return c
    return c


for d in ALL_DAYS:
    t = (d - START).days
    n = DAILY_ORDERS[d]
    times = sorted(order_time(d) for _ in range(n))
    p_new = 0.135 + 0.75 * math.exp(-t / 25.0) + (0.08 if is_bfcm(d) else 0.0)
    for at in times:
        o = {"created_at": at, "date": d, "guest": False, "test": False}
        if t > 30 and rng.random() < 0.002:
            o["test"] = True
            o["customer"] = rng.choice(STAFF)
            o["account"] = o["customer"]
            o["email"] = o["customer"]["email"]
        elif rng.random() < 0.04 and tickets:
            # Guest checkout: no customer_id. Some are existing customers, some are one-off buyers.
            o["guest"] = True
            if rng.random() < 0.55:
                c = pick_existing(at)
                o["customer"], o["account"] = c, None
                o["email"] = casing_variant(c["email"]) if rng.random() < 0.3 else c["email"]
            else:
                first, last = rng.choice(FIRST), rng.choice(LAST)
                o["customer"], o["account"] = None, None
                o["email"] = new_email(first.lower(), last.lower())
                o["guest_name"] = (first, last)
                o["guest_country"] = pick([(c, v[0]) for c, v in COUNTRIES.items()])
        elif not tickets or rng.random() < p_new:
            c = make_customer(at - timedelta(minutes=rng.randint(1, 15)))
            # A large share buy once and never return; the rest lapse after an exponential lifetime.
            c["active_until"] = at if rng.random() < 0.42 else at + timedelta(days=rng.expovariate(1 / 420))
            if c["loyalty"] >= 8:
                c["active_until"] = at + timedelta(days=3650)
            first_order_index.append(c)
            for _ in range(c["loyalty"]):
                tickets.append(c)
            o["customer"] = o["account"] = c
            o["email"] = c["email"]
        else:
            c = pick_existing(at)
            account = c
            if c["twin_planned"] and c["orders"]:
                if c["twin"] is None:
                    tw = make_customer(at - timedelta(minutes=rng.randint(1, 30)), first=c["first_name"],
                                       last=c["last_name"], email=casing_variant(c["email"]), country=c["country"])
                    tw.update(region=c["region"], tax_bp=c["tax_bp"], currency=c["currency"], city=c["city"],
                              zip=c["zip"], street=c["street"], twin_planned=False, loyalty=1, canonical=c,
                              redacted=False, tags=[])
                    c["twin"] = tw
                if rng.random() < 0.45:
                    account = c["twin"]
            o["customer"], o["account"] = c, account
            o["email"] = account["email"]
        orders.append(o)
        if o.get("customer") is not None and not o["test"]:
            o["customer"]["orders"].append(o)
    first_order_prefix[d] = len(first_order_index)

# Accounts that signed up (newsletter, wishlist) but never ordered; some spam signups get deleted.
for _ in range(NEVER_ORDERED_ACCOUNTS):
    d = rng.choice(ALL_DAYS)
    c = make_customer(local_dt(d, rng.randrange(86_400)))
    c["twin_planned"] = False
    c["accepts_marketing"] = rng.random() < 0.85
    if rng.random() < 0.18:
        c["deleted"] = True
        c["email"] = f"{rng.choice(['xx', 'qq', 'promo', 'win'])}{rng.randint(1000, 99999)}@{rng.choice(['mail.ru', 'yandex.ru', 'tempmail.dev'])}"

orders.sort(key=lambda o: o["created_at"])

# ---- line items, totals, lifecycle

order_lines: list[dict] = []
refunds: list[dict] = []
transactions: list[dict] = []
stripe_charges: list[dict] = []
stripe_refunds: list[dict] = []
_oid = 5_600_000_000_000
_lid = 14_000_000_000_000
_rid = 900_000_000_000
_tid = 6_800_000_000_000
LINES_PER_ORDER = [(1, 38), (2, 26), (3, 16), (4, 9), (5, 5), (6, 3), (7, 2), (8, 1)]
SOURCES = [("web", 74), ("iphone", 9), ("android", 4), ("pos", 5), ("shopify_draft_order", 1.5), ("294517", 6.5)]
CANCEL_REASONS = [("customer", 60), ("inventory", 15), ("fraud", 10), ("declined", 8), ("other", 7)]


def stripe_id(prefix: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(rng.choice(alphabet) for _ in range(24))


def add_txn(o, kind, status, gateway, amount, at, parent=None, refund_id=None, authorization=None,
            error_code=None):
    global _tid
    _tid += rng.randint(1_000, 400_000)
    tx = {"id": _tid, "order_id": o["id"], "refund_id": refund_id, "parent_id": parent["id"] if parent else None,
          "kind": kind, "status": status, "gateway": gateway, "amount": amount, "currency": "USD",
          "created_at": at, "processed_at": at, "authorization": authorization, "error_code": error_code,
          "test": o["test"], "presentment_currency": o["presentment_currency"]}
    transactions.append(tx)
    return tx


for number, o in enumerate(orders, start=1001):
    _oid += rng.randint(10_000, 2_000_000)
    o["id"], o["number"] = _oid, number
    d, at = o["date"], o["created_at"]
    c = o["customer"]
    acct = o["account"]
    if c is not None:
        country, region, tax_bp = c["country"], c["region"], c["tax_bp"]
    else:
        country = o["guest_country"]
        region, tax_bp = pick([((r, tx), w) for r, tx, w in COUNTRIES[country][2]])
    o["country"], o["region"] = country, region
    o["presentment_currency"] = COUNTRIES[country][1]
    o["source"] = "web" if o["test"] else pick(SOURCES)
    if country != "US" and o["source"] == "pos":
        o["source"] = "web"
    o["deleted"] = False
    if o["source"] == "shopify_draft_order" and rng.random() < 0.12:
        o["deleted"] = True  # duplicate draft orders the team deleted
    is_first = c is not None and c["orders"] and c["orders"][0] is o
    o["is_first"] = bool(is_first)
    o["channel"] = channel_for(c if not is_first else None, d)
    if is_first:
        c["acq_channel"] = o["channel"]

    # lines
    n_lines = pick(LINES_PER_ORDER)
    if is_bfcm(d) and rng.random() < 0.4:
        n_lines += 1
    chosen: dict[int, dict] = {}
    for _ in range(n_lines):
        p = pick_product(d, at)
        v = pick_variant(p)
        if v["id"] in chosen:
            chosen[v["id"]]["qty"] += 1
            continue
        qty = pick([(1, 82), (2, 13), (3, 4), (4, 0.6), (6, 0.4)])
        if p["category"] == "Gift Cards":
            qty = 1
        chosen[v["id"]] = {"product": p, "variant": v, "qty": qty}
    lines = list(chosen.values())
    if rng.random() < 0.02 and o["source"] != "pos":
        lines.append({"product": None, "variant": None, "qty": 1, "custom": "Gift Wrap", "price": 500})
    for ln in lines:
        if ln["product"] is not None:
            ln["price"] = ln["variant"]["price"]
            if is_summer_sale(d) and ln["product"]["category"] in ("Tops", "Camp & Home") and rng.random() < 0.5:
                ln["price"] = ln["price"] * 80 // 100  # automatic sale price, no code

    line_total = sum(ln["price"] * ln["qty"] for ln in lines)
    gift_total = sum(ln["price"] * ln["qty"] for ln in lines
                     if ln["product"] is not None and ln["product"]["category"] == "Gift Cards")
    discountable = line_total - gift_total

    # discount code
    code = None
    if o["test"]:
        code = None
    elif is_bfcm(d) and d.year in BFCM and rng.random() < 0.62:
        code = BFCM[d.year]
    elif is_summer_sale(d) and d.year in SUMMER and rng.random() < 0.3:
        code = SUMMER[d.year]
    elif o["is_first"] and rng.random() < 0.33:
        code = WELCOME
    elif c is not None and "vip" in c["tags"] and rng.random() < 0.25:
        code = VIP_CODE
    elif rng.random() < 0.07:
        code = creator_code(at)
    discount = 0
    if code is not None and discountable > 0:
        if code["value_type"] == "percentage":
            discount = discountable * code["value"] // 100
        else:
            discount = min(code["value"], discountable)
        code["usage_count"] += 1
    o["discount_code"] = code
    # allocate the order discount across discountable lines; the last line takes the remainder
    disc_lines = [ln for ln in lines if not (ln["product"] and ln["product"]["category"] == "Gift Cards")]
    remaining = discount
    for i, ln in enumerate(disc_lines):
        share = remaining if i == len(disc_lines) - 1 else (discount * ln["price"] * ln["qty"] // max(discountable, 1))
        ln["discount"] = share
        remaining -= share
    for ln in lines:
        ln.setdefault("discount", 0)

    subtotal = line_total - discount
    physical = discountable > 0
    if o["source"] == "pos" or not physical:
        shipping = 0
    elif country == "US":
        shipping = 0 if subtotal >= 7_500 else 795
    else:
        shipping = 0 if subtotal >= 15_000 else 1_495
    taxable = subtotal - gift_total
    tax = (taxable * tax_bp + 5_000) // 10_000
    o.update(lines=lines, line_total=line_total, discount=discount, subtotal=subtotal, shipping=shipping,
             tax=tax, total=subtotal + shipping + tax)

    # gateway
    if o["test"]:
        gateway = "bogus"
    elif o["source"] == "pos":
        gateway = pick([("shopify_payments", 72), ("cash", 28)])
    elif o["source"] == "shopify_draft_order":
        gateway = pick([("manual", 60), ("shopify_payments", 40)])
    else:
        gateway = pick([("shopify_payments", 77), ("paypal", 17), ("shopify_installments", 3), ("gift_card", 3)])
    o["gateway"] = gateway

    # lifecycle
    age_h = (CUTOFF - at).total_seconds() / 3600
    events = [at]
    o.update(cancelled_at=None, cancel_reason=None, financial="paid", fulfillment=None, fulfilled_at=None,
             closed_at=None, refunds=[])
    auth_style = gateway == "shopify_payments" and o["source"] != "pos"
    roll = rng.random()
    if o["deleted"]:
        o["financial"] = "pending"
    elif not o["test"] and roll < 0.021 and age_h > 12:
        o["cancelled_at"] = at + timedelta(hours=rng.uniform(0.2, 40))
        o["cancel_reason"] = pick(CANCEL_REASONS)
        o["financial"] = "voided" if auth_style else "refunded"
        events.append(o["cancelled_at"])
    elif gateway == "manual" and rng.random() < 0.08:
        o["financial"] = "pending"  # net-30 draft order, payment not yet received
    else:
        if auth_style and age_h < 30:
            o["financial"] = "authorized"
        if o["source"] == "pos":
            o["fulfilled_at"] = at
        else:
            ff = at + timedelta(hours=rng.uniform(8, 80) * (2.2 if is_bfcm(d) else 1.0))
            if ff < CUTOFF and o["financial"] != "authorized":
                o["fulfilled_at"] = ff
        if o["fulfilled_at"] is not None:
            o["fulfillment"] = "fulfilled" if rng.random() > 0.015 or len(lines) < 2 else "partial"
            events.append(o["fulfilled_at"])
            units = sum(ln["qty"] for ln in lines)
            r = rng.random()
            refund_at = o["fulfilled_at"] + timedelta(days=rng.uniform(5, 40))
            if r < 0.022 and refund_at < CUTOFF and not o["test"]:
                o["refunds"].append({"at": refund_at, "kind": "full"})
            elif r < 0.07 and units >= 2 and refund_at < CUTOFF and not o["test"]:
                o["refunds"].append({"at": refund_at, "kind": "partial"})
            if o["source"] != "pos" and o["fulfillment"] == "fulfilled":
                o["closed_at"] = o["fulfilled_at"] + timedelta(minutes=rng.randint(1, 120))
        elif o["financial"] == "paid" and auth_style:
            o["financial"] = "authorized"

    # line rows
    for ln in lines:
        _lid += rng.randint(1_000, 300_000)
        ln["id"] = _lid

    # refunds -> amounts
    refunded_total = 0
    for rf in o["refunds"]:
        _rid += rng.randint(10_000, 900_000)
        rf["id"] = _rid
        if rf["kind"] == "full":
            amount = o["total"]
            rl = [{"line_item_id": ln["id"], "quantity": ln["qty"],
                   "subtotal": money(ln["price"] * ln["qty"] - ln["discount"])} for ln in lines]
            o["financial"] = "refunded"
        else:
            cand = [ln for ln in lines if ln["product"] is not None]
            ln = rng.choice(cand)
            unit_net = ln["price"] - ln["discount"] // ln["qty"]
            unit_tax = (unit_net * tax_bp + 5_000) // 10_000 if ln["product"]["category"] != "Gift Cards" else 0
            amount = unit_net + unit_tax
            rl = [{"line_item_id": ln["id"], "quantity": 1, "subtotal": money(unit_net)}]
            o["financial"] = "partially_refunded"
        rf.update(amount=amount, lines=rl, restock=rng.random() < 0.7,
                  note=rng.choice([None, None, "Return - wrong size", "Return - didn't fit", "Damaged in transit",
                                   "Return - changed mind", "Exchange", "Late delivery"]))
        refunded_total += amount
        events.append(rf["at"])
    o["refunded_total"] = refunded_total
    o["updated_at"] = max(events) + timedelta(seconds=rng.randint(1, 600))

    # transactions + stripe
    pres = o["presentment_currency"]
    if o["deleted"]:
        pass
    elif o["financial"] == "pending":
        add_txn(o, "sale", "pending", gateway, o["total"], at)
    else:
        if rng.random() < 0.035 and gateway in ("shopify_payments", "paypal"):
            fail_at = at - timedelta(seconds=rng.randint(20, 600))
            ch_id = stripe_id("ch_") if gateway == "shopify_payments" else None
            add_txn(o, "authorization" if auth_style else "sale", "failure", gateway, o["total"], fail_at,
                    authorization=ch_id, error_code=pick([("card_declined", 70), ("incorrect_cvc", 12),
                                                          ("expired_card", 10), ("processing_error", 8)]))
            if ch_id:
                stripe_charges.append({"id": ch_id, "order": o, "amount": o["total"], "created": fail_at,
                                       "status": "failed", "captured": False, "captured_at": None,
                                       "failure": "card_declined"})
        if auth_style:
            ch_id = stripe_id("ch_")
            auth = add_txn(o, "authorization", "success", gateway, o["total"], at, authorization=ch_id)
            ch = {"id": ch_id, "order": o, "amount": o["total"], "created": at, "status": "succeeded",
                  "captured": False, "captured_at": None, "failure": None, "voided_at": None}
            stripe_charges.append(ch)
            if o["financial"] == "voided":
                add_txn(o, "void", "success", gateway, o["total"], o["cancelled_at"], parent=auth, authorization=ch_id)
                ch["voided_at"] = o["cancelled_at"]
            elif o["financial"] != "authorized":
                cap_at = o["fulfilled_at"] or at
                cap = add_txn(o, "capture", "success", gateway, o["total"], cap_at, parent=auth, authorization=ch_id)
                ch["captured"], ch["captured_at"] = True, cap_at
                o["sale_txn"] = cap
            o["charge"] = ch
        else:
            ch_id = stripe_id("ch_") if gateway == "shopify_payments" else None
            sale_at = at
            sale = add_txn(o, "sale", "success", gateway, o["total"], sale_at, authorization=ch_id or (
                f"PAYID-{rng.getrandbits(48):012X}" if gateway == "paypal" else None))
            o["sale_txn"] = sale
            if ch_id:
                ch = {"id": ch_id, "order": o, "amount": o["total"], "created": sale_at, "status": "succeeded",
                      "captured": True, "captured_at": sale_at, "failure": None, "voided_at": None}
                stripe_charges.append(ch)
                o["charge"] = ch
        if o["financial"] == "refunded" and o["cancelled_at"] is not None and not o["refunds"]:
            # cancelled after payment on a sale-style gateway: refund in full at cancellation
            _rid += rng.randint(10_000, 900_000)
            o["refunds"].append({"id": _rid, "at": o["cancelled_at"] + timedelta(minutes=rng.randint(1, 90)),
                                 "kind": "full", "amount": o["total"],
                                 "lines": [{"line_item_id": ln["id"], "quantity": ln["qty"],
                                            "subtotal": money(ln["price"] * ln["qty"] - ln["discount"])} for ln in lines],
                                 "restock": True, "note": "Order cancelled"})
            o["refunded_total"] = o["total"]
        for rf in o["refunds"]:
            rtx = add_txn(o, "refund", "success", gateway, rf["amount"], rf["at"], parent=o.get("sale_txn"),
                          refund_id=rf["id"], authorization=o["charge"]["id"] if o.get("charge") else None)
            rf["txn"] = rtx
            refunds.append({"order": o, **rf})
            if o.get("charge") is not None:
                stripe_refunds.append({"id": stripe_id("re_"), "charge": o["charge"], "amount": rf["amount"],
                                       "created": rf["at"], "shopify_refund_id": rf["id"],
                                       "reason": "requested_by_customer" if o["cancelled_at"] is None else (
                                           "fraudulent" if o["cancel_reason"] == "fraud" else "requested_by_customer")})

# --------------------------------------------------------------------------- sessions

sessions: list[dict] = []
CAMPAIGNS = {
    "google_ads": ["Brand Search", "Non-Brand Search", "Shopping - All Products", "Performance Max"],
    "meta_ads": ["Prospecting - Broad", "Retargeting - 30d", "Advantage+ Shopping"],
    "tiktok_ads": ["Spark Ads - UGC", "Prospecting - Interests"],
    "pinterest_ads": ["Catalog Shopping"],
}
UTM_SOURCE = {
    "google_ads": [("google", 80), ("Google", 10), ("adwords", 10)],
    "meta_ads": [("facebook", 40), ("instagram", 30), ("fb", 12), ("ig", 10), ("Facebook", 8)],
    "tiktok_ads": [("tiktok", 90), ("TikTok", 10)],
    "pinterest_ads": [("pinterest", 100)],
    "email": [("klaviyo", 92), ("Klaviyo", 8)],
}
UTM_MEDIUM = {
    "google_ads": [("cpc", 100)],
    "meta_ads": [("paid_social", 70), ("paidsocial", 15), ("cpc", 15)],
    "tiktok_ads": [("paid_social", 100)],
    "pinterest_ads": [("paid_social", 80), ("cpc", 20)],
    "email": [("email", 100)],
}
LANDING = ["/", "/", "/collections/new-arrivals", "/collections/outerwear", "/collections/tops",
           "/collections/sale", "/pages/our-story", "/blogs/field-notes/packing-list"]


def campaign_for(ch: str, d: date) -> str | None:
    if ch in CAMPAIGNS:
        if is_bfcm(d) and ch in ("google_ads", "meta_ads") and rng.random() < 0.5:
            return f"BFCM {d.year}"
        return rng.choice(CAMPAIGNS[ch])
    if ch == "email":
        if is_bfcm(d):
            return rng.choice([f"bfcm_{d.year}_early_access", f"bfcm_{d.year}_last_chance"])
        return rng.choice(["welcome_series", "abandoned_cart", "post_purchase", "winback_90d",
                           f"newsletter_{d.isoformat()}"])
    if ch == "affiliate":
        return "creator_program"
    return None


def make_session(ch: str, started: datetime, d: date, customer_id, anon, converted_order=None) -> dict:
    src = med = camp = ref = None
    if ch in UTM_SOURCE:
        src, med = pick(UTM_SOURCE[ch]), pick(UTM_MEDIUM[ch])
        camp = campaign_for(ch, d)
        if camp and rng.random() < 0.15:
            camp = camp.lower().replace(" - ", "_").replace(" ", "_")
    elif ch == "affiliate":
        src, med, camp = rng.choice(_handles).lower(), "affiliate", "creator_program"
    elif ch == "organic_search":
        ref = rng.choice(["https://www.google.com/", "https://www.google.com/", "https://www.bing.com/",
                          "https://duckduckgo.com/"])
    elif ch == "organic_social":
        ref = rng.choice(["https://www.instagram.com/", "https://l.instagram.com/", "https://www.reddit.com/",
                          "https://www.youtube.com/"])
    device = pick([("mobile", 64), ("desktop", 31), ("tablet", 5)])
    pv = max(1, int(rng.expovariate(1 / 3.5))) + (4 if converted_order else 0)
    return {
        "session_id": rid(), "anonymous_id": anon, "customer_id": customer_id, "started_at": started,
        "duration": int(pv * rng.uniform(20, 90)), "landing_page": rng.choice(LANDING), "referrer": ref,
        "utm_source": src, "utm_medium": med, "utm_campaign": camp, "device_type": device,
        "pageviews": pv, "converted_order_id": converted_order["id"] if converted_order else None, "channel": ch,
        "country": converted_order["country"] if converted_order else pick([(c, v[0]) for c, v in COUNTRIES.items()]),
    }


orders_by_day: dict[date, int] = {}
for o in orders:
    orders_by_day[o["date"]] = orders_by_day.get(o["date"], 0) + 1
    if o["test"] or o["deleted"] or o["source"] not in ("web", "iphone", "android"):
        continue
    if rng.random() < 0.13:
        continue  # ad-blocked / consent-declined: no tracked session
    acct = o["account"]
    sess = make_session(o["channel"], o["created_at"] - timedelta(minutes=rng.randint(3, 50)), o["date"],
                        acct["id"] if acct is not None and rng.random() < 0.7 else None,
                        o["customer"]["anon_id"] if o["customer"] is not None else rid(), converted_order=o)
    sessions.append(sess)

_sess_weight = {d: orders_by_day.get(d, 0) ** 0.85 for d in ALL_DAYS}
_sw_total = sum(_sess_weight.values())
for d in ALL_DAYS:
    n = round(NON_CONVERTING_SESSIONS * _sess_weight[d] / _sw_total)
    known = first_order_prefix.get(d - timedelta(days=1), 0)
    for _ in range(n):
        started = local_dt(d, rng.choices(range(24), weights=HOUR_W, k=1)[0] * 3600 + rng.randrange(3600))
        cust = first_order_index[rng.randrange(known)] if known and rng.random() < 0.2 else None
        ch = pick(REPEAT_MIX) if cust else pick(ACQ_MIX)
        sessions.append(make_session(ch, started, d, cust["id"] if cust and rng.random() < 0.4 else None,
                                     cust["anon_id"] if cust else rid()))
sessions.sort(key=lambda s: s["started_at"])


# --------------------------------------------------------------------------- order validity (oracle)


def net_revenue(o: dict) -> int:
    if o["cancelled_at"] is not None or o["financial"] in ("voided", "refunded"):
        return 0
    return o["total"] - o["tax"]


def is_valid(o: dict) -> bool:
    return not (o["cancelled_at"] is not None or o["financial"] in ("voided", "refunded"))


# --------------------------------------------------------------------------- ad spend

ROAS_TARGET = {"google_ads": 4.2, "meta_ads": 2.7, "tiktok_ads": 1.5, "pinterest_ads": 1.9}
FLOOR = {"google_ads": 260_00, "meta_ads": 220_00, "tiktok_ads": 60_00, "pinterest_ads": 25_00}
CPM = {"google_ads": 900, "meta_ads": 1150, "tiktok_ads": 620, "pinterest_ads": 780}  # cents per 1000 impressions
CTR = {"google_ads": 0.045, "meta_ads": 0.011, "tiktok_ads": 0.008, "pinterest_ads": 0.006}
attr_rev: dict[tuple[date, str], int] = {}
attr_orders: dict[tuple[date, str], int] = {}
_order_by_id = {o["id"]: o for o in orders}
for s in sessions:
    if s["converted_order_id"] is None or s["channel"] not in ROAS_TARGET:
        continue
    o = _order_by_id[s["converted_order_id"]]
    k = (o["date"], s["channel"])
    attr_rev[k] = attr_rev.get(k, 0) + net_revenue(o)
    attr_orders[k] = attr_orders.get(k, 0) + 1

ad_spend: list[dict] = []
CAMPAIGN_IDS: dict[tuple[str, str], str] = {}
ACCOUNT_IDS = {"google_ads": "382-551-9024", "meta_ads": "act_1183920457731", "tiktok_ads": "7231098812345",
               "pinterest_ads": "549762113908"}
for d in ALL_DAYS:
    for ch in PAID_CHANNELS:
        if ch == "pinterest_ads" and d < date(2025, 3, 1):
            continue  # Pinterest launched in March 2025
        rev = attr_rev.get((d, ch), 0)
        total = max(FLOOR[ch], rev / ROAS_TARGET[ch]) * math.exp(rng.gauss(0, 0.12))
        names = list(CAMPAIGNS[ch]) + ([f"BFCM {d.year}"] if is_bfcm(d) and ch in ("google_ads", "meta_ads") else [])
        shares = [rng.uniform(0.5, 1.5) for _ in names]
        for name, sh in zip(names, shares):
            if (ch, name) not in CAMPAIGN_IDS:
                CAMPAIGN_IDS[(ch, name)] = str(rng.randint(10**10, 10**11 - 1))
            spend = int(total * sh / sum(shares))
            impressions = max(1, int(spend / CPM[ch] * 1000 * rng.uniform(0.85, 1.15)))
            clicks = int(impressions * CTR[ch] * rng.uniform(0.7, 1.3))
            conv = attr_orders.get((d, ch), 0) * sh / sum(shares) * rng.uniform(1.0, 1.6)
            loaded = datetime(d.year, d.month, d.day, 7, 0, tzinfo=UTC) + timedelta(days=1, minutes=rng.randint(0, 90))
            row = {"date": d, "channel": ch, "account_id": ACCOUNT_IDS[ch], "campaign_id": CAMPAIGN_IDS[(ch, name)],
                   "campaign_name": name, "impressions": impressions, "clicks": clicks, "spend": spend,
                   "currency": "USD", "conversions": round(conv, 2),
                   "conversion_value": round(conv * (rev / max(attr_orders.get((d, ch), 1), 1)) / 100, 2),
                   "loaded_at": loaded}
            ad_spend.append(row)
            if rng.random() < 0.04 and loaded + timedelta(days=1) < CUTOFF:
                # Platform restatement: the same day/campaign lands again with revised numbers.
                re = dict(row)
                re["spend"] = int(spend * (1 + rng.gauss(0, 0.06)))
                re["clicks"] = int(clicks * rng.uniform(0.95, 1.05))
                re["loaded_at"] = loaded + timedelta(days=rng.randint(1, 3), minutes=rng.randint(0, 600))
                ad_spend.append(re)


# --------------------------------------------------------------------------- stripe payouts

payouts: dict[date, dict] = {}


def payout_for(at: datetime) -> dict:
    arrival = (at.astimezone(UTC) + timedelta(days=2)).date()
    if arrival.weekday() >= 5:
        arrival += timedelta(days=7 - arrival.weekday())
    if arrival not in payouts:
        payouts[arrival] = {"id": None, "arrival": arrival, "amount": 0, "count": 0}
    return payouts[arrival]


for ch in stripe_charges:
    ch["fee"] = 0
    ch["payout"] = None
    if ch["captured"]:
        intl = ch["order"]["country"] != "US"
        ch["fee"] = (ch["amount"] * (39 if intl else 29) + 500) // 1000 + 30
        po = payout_for(ch["captured_at"])
        po["amount"] += ch["amount"] - ch["fee"]
        po["count"] += 1
        ch["payout"] = po
for rf in stripe_refunds:
    po = payout_for(rf["created"])
    po["amount"] -= rf["amount"]
    rf["payout"] = po

# Non-Shopify Stripe charges: wholesale invoices paid by card, no Shopify order behind them.
for i in range(240):
    d = rng.choice(ALL_DAYS)
    at = local_dt(d, rng.randrange(9 * 3600, 17 * 3600))
    amount = rng.randint(400, 6_000) * 100
    ch = {"id": stripe_id("ch_"), "order": None, "amount": amount, "created": at, "status": "succeeded",
          "captured": True, "captured_at": at, "failure": None, "voided_at": None,
          "fee": (amount * 29 + 500) // 1000 + 30, "wholesale": f"INV-{2024 + i // 120}-{i % 120 + 1:04d}"}
    po = payout_for(at)
    po["amount"] += amount - ch["fee"]
    ch["payout"] = po
    stripe_charges.append(ch)

for po in sorted(payouts.values(), key=lambda p: p["arrival"]):
    po["id"] = stripe_id("po_")


# --------------------------------------------------------------------------- writers


def write(name: str, header: list[str], rows) -> int:
    path = OUT / f"{name}.csv"
    n = 0
    with path.open("w", newline="", encoding="utf8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def synced(at: datetime, late: bool = False) -> str:
    lag = timedelta(days=rng.uniform(2, 9)) if late else timedelta(minutes=rng.randint(5, 360))
    return ts_utc(min(at + lag, CUTOFF))


def address(c_or_o: dict, first: str | None, last: str | None) -> dict:
    return {"first_name": first, "last_name": last, "address1": c_or_o["street"], "city": c_or_o["city"],
            "province_code": c_or_o["region"], "country_code": c_or_o["country"], "zip": c_or_o["zip"]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    counts = {}

    # ---- raw_shopify.customers (append-mode: stale prior versions for some accounts)
    acct_orders: dict[int, list[dict]] = {}
    for o in orders:
        if o.get("account") is not None and not o["deleted"]:
            acct_orders.setdefault(o["account"]["id"], []).append(o)

    def _customer_versions(c):
        mine = acct_orders.get(c["id"], [])
        spent = sum(o["total"] - o["refunded_total"] for o in mine if o["financial"] != "voided")
        last_at = max([o["updated_at"] for o in mine], default=c["created_at"])
        redacted = c["redacted"]
        addr = None if redacted else {
            "address1": c["street"], "city": c["city"], "province_code": c["region"], "country_code": c["country"],
            "zip": c["zip"], "default": True}
        current = {
            "id": c["id"], "email": None if redacted else c["email"],
            "first_name": None if redacted else c["first_name"], "last_name": None if redacted else c["last_name"],
            "phone": None if redacted else c["phone"], "state": "disabled" if not mine else "enabled",
            "verified_email": not redacted, "accepts_marketing": c["accepts_marketing"],
            "currency": c["currency"], "orders_count": len(mine), "total_spent": money(spent),
            "tags": ", ".join(c["tags"] + (["redacted"] if redacted else [])) or None,
            "default_address": addr, "created_at": c["created_at"], "updated_at": last_at,
            "deleted": c["deleted"],
        }
        if len(mine) >= 2 and rng.random() < 0.12:
            old = dict(current)
            k = rng.randint(1, len(mine) - 1)
            old.update(orders_count=k, total_spent=money(sum(o["total"] for o in mine[:k])),
                       updated_at=mine[k - 1]["updated_at"], deleted=False,
                       email=c["email"], first_name=c["first_name"], last_name=c["last_name"])
            yield _cust_row(old)
        yield _cust_row(current)

    def _cust_row(v):
        return [v["id"], v["email"], v["first_name"], v["last_name"], v["phone"], v["state"],
                boolstr(v["verified_email"]), boolstr(v["accepts_marketing"]), v["currency"], v["orders_count"],
                v["total_spent"], v["tags"], jdump(v["default_address"]), ts_local(v["created_at"]),
                ts_local(v["updated_at"]), synced(v["updated_at"]), boolstr(v["deleted"])]

    counts["raw_shopify.customers"] = write(
        "raw_shopify.customers",
        ["id", "email", "first_name", "last_name", "phone", "state", "verified_email", "accepts_marketing",
         "currency", "orders_count", "total_spent", "tags", "default_address", "created_at", "updated_at",
         "_fivetran_synced", "_fivetran_deleted"],
        (r for c in customers for r in _customer_versions(c)))

    # ---- raw_shopify.products / product_variants
    def product_rows():
        for p in products:
            if p["missing_from_raw"]:
                continue
            upd = p["archived_at"] or p["created_at"] + timedelta(days=rng.randint(0, 200))
            if upd > CUTOFF:
                upd = CUTOFF - timedelta(hours=rng.randint(1, 48))
            tags = [p["category"].lower().replace(" & ", "-"), rng.choice(["core", "seasonal", "limited", "bestseller"])]
            yield [p["id"], p["title"], p["handle"], p["vendor"], p["category"], p["status"],
                   ", ".join(tags), None if p["status"] == "draft" else ts_local(p["created_at"] + timedelta(hours=2)),
                   ts_local(p["created_at"]), ts_local(upd), synced(upd), boolstr(p["deleted"])]

    counts["raw_shopify.products"] = write(
        "raw_shopify.products",
        ["id", "title", "handle", "vendor", "product_type", "status", "tags", "published_at", "created_at",
         "updated_at", "_fivetran_synced", "_fivetran_deleted"], product_rows())

    def variant_rows():
        for p in products:
            if p["missing_from_raw"]:
                continue
            for v in p["variants"]:
                yield [v["id"], p["id"], v["title"], v["sku"], money(v["price"]), money(v["compare_at"]),
                       v["color"], v["size"], v["inventory"], v["weight_grams"],
                       boolstr(p["category"] != "Gift Cards"), ts_local(v["created_at"]),
                       ts_local(v["created_at"] + timedelta(days=rng.randint(0, 300))),
                       synced(v["created_at"] + timedelta(days=300)), boolstr(p["deleted"])]

    counts["raw_shopify.product_variants"] = write(
        "raw_shopify.product_variants",
        ["id", "product_id", "title", "sku", "price", "compare_at_price", "option1", "option2",
         "inventory_quantity", "grams", "requires_shipping", "created_at", "updated_at", "_fivetran_synced",
         "_fivetran_deleted"], variant_rows())

    # ---- raw_shopify.discounts
    counts["raw_shopify.discounts"] = write(
        "raw_shopify.discounts",
        ["id", "code", "title", "value_type", "value", "target_type", "once_per_customer", "usage_count",
         "starts_at", "ends_at", "created_at", "updated_at", "_fivetran_synced"],
        ([d["id"], d["code"], d["title"], d["value_type"],
          money(-d["value"]) if d["value_type"] == "fixed_amount" else f"-{d['value']}.0", "line_item",
          boolstr(d["kind"] == "welcome"), d["usage_count"], ts_local(d["starts_at"]),
          ts_local(d["ends_at"]) if d["ends_at"] else None, ts_local(d["created_at"]),
          ts_local(min(CUTOFF, d["ends_at"] or CUTOFF)), synced(min(CUTOFF, d["ends_at"] or CUTOFF))]
         for d in discounts))

    # ---- raw_shopify.orders (append-mode: some orders also carry an earlier version)
    def price_set(cents: int, o: dict) -> dict:
        rate = FX[o["presentment_currency"]] * (1 + 0.02 * math.sin((o["date"] - START).days / 60))
        return {"shop_money": {"amount": money(cents), "currency_code": "USD"},
                "presentment_money": {"amount": money(round(cents * rate)),
                                      "currency_code": o["presentment_currency"]}}

    def order_row(o, v):
        c, acct = o["customer"], o["account"]
        if c is not None:
            first, last = c["first_name"], c["last_name"]
            addr_src = c
        else:
            first, last = o["guest_name"]
            if "guest_addr" not in o:
                o["guest_addr"] = {"street": f"{rng.randint(1, 9999)} {rng.choice(STREETS)}",
                                   "city": rng.choice(CITIES[o["country"]]), "region": o["region"],
                                   "country": o["country"], "zip": f"{rng.randint(10000, 99999)}"}
            addr_src = o["guest_addr"]
        redacted = acct is not None and acct["redacted"]
        ship = None if o["source"] == "pos" or redacted else address(addr_src, first, last)
        bill = None if redacted else {"country_code": o["country"], "zip": addr_src["zip"]}
        code = o["discount_code"]
        dcodes = [] if code is None else [{"code": code["code"], "type": code["value_type"],
                                           "amount": money(o["discount"])}]
        notes = []
        if o["is_first"] and rng.random() < 0.3:
            notes.append({"name": "how_did_you_hear", "value": rng.choice(
                ["Instagram", "A friend", "Podcast", "Google", "TikTok", "instagram", "friend"])})
        if o["channel"] == "affiliate":
            notes.append({"name": "ref", "value": rng.choice(_handles).lower()})
        landing = f"/?utm_source={o['channel']}" if o["channel"] in PAID_CHANNELS and rng.random() < 0.5 else "/"
        return [o["id"], f"#{o['number']}", o["number"], acct["id"] if acct is not None else None,
                None if redacted else o["email"], ts_local(o["created_at"]), ts_local(o["created_at"]),
                ts_local(v["updated_at"]), ts_local(v["cancelled_at"]) if v["cancelled_at"] else None,
                v["cancel_reason"], ts_local(v["closed_at"]) if v["closed_at"] else None, "USD",
                o["presentment_currency"], money(o["line_total"]), money(o["subtotal"]), money(o["discount"]),
                money(o["tax"]), money(o["total"]), jdump(price_set(o["total"], o)),
                jdump(price_set(o["shipping"], o)), v["financial"], v["fulfillment"], o["source"],
                boolstr(o["test"]), landing, None if o["source"] == "pos" else rng.choice(
                    [None, "https://www.google.com/", "https://www.instagram.com/", "android-app://com.google.android.gm/"]),
                ", ".join(t for t in (["first-order"] if o["is_first"] else []) + (
                    ["wholesale"] if o["source"] == "shopify_draft_order" else [])) or None,
                jdump(notes) if notes else None, jdump(ship), jdump(bill), jdump(dcodes),
                v["synced"], boolstr(v["deleted"])]

    def order_rows():
        for o in orders:
            current = {"updated_at": o["updated_at"], "cancelled_at": o["cancelled_at"],
                       "cancel_reason": o["cancel_reason"], "closed_at": o["closed_at"],
                       "financial": o["financial"], "fulfillment": o["fulfillment"], "deleted": o["deleted"],
                       "synced": synced(o["updated_at"], late=rng.random() < 0.006)}
            changed = o["updated_at"] - o["created_at"] > timedelta(hours=2)
            if changed and (o["refunds"] or o["cancelled_at"] or rng.random() < 0.12):
                first_fin = "authorized" if o["gateway"] == "shopify_payments" and o["source"] != "pos" else (
                    "pending" if o["financial"] == "pending" else "paid")
                stale = {"updated_at": o["created_at"] + timedelta(seconds=rng.randint(5, 90)), "cancelled_at": None,
                         "cancel_reason": None, "closed_at": None, "financial": first_fin, "fulfillment": None,
                         "deleted": False, "synced": synced(o["created_at"])}
                if o["refunds"] and o["fulfilled_at"] is not None:
                    stale.update(financial="paid", fulfillment=o["fulfillment"], closed_at=o["closed_at"],
                                 updated_at=o["closed_at"] or o["fulfilled_at"],
                                 synced=synced(o["closed_at"] or o["fulfilled_at"]))
                yield order_row(o, stale)
            yield order_row(o, current)

    counts["raw_shopify.orders"] = write(
        "raw_shopify.orders",
        ["id", "name", "order_number", "customer_id", "email", "created_at", "processed_at", "updated_at",
         "cancelled_at", "cancel_reason", "closed_at", "currency", "presentment_currency",
         "total_line_items_price", "subtotal_price", "total_discounts", "total_tax", "total_price",
         "total_price_set", "total_shipping_price_set", "financial_status", "fulfillment_status", "source_name",
         "test", "landing_site", "referring_site", "tags", "note_attributes", "shipping_address",
         "billing_address", "discount_codes", "_fivetran_synced", "_fivetran_deleted"], order_rows())

    def line_rows():
        for o in orders:
            for idx, ln in enumerate(o["lines"]):
                p, v = ln["product"], ln["variant"]
                is_gc = p is not None and p["category"] == "Gift Cards"
                props = None
                if p is not None and p["category"] in ("Bags", "Accessories") and rng.random() < 0.03:
                    props = [{"name": "Monogram", "value": "".join(rng.choice("ABCDEFGHJKLMNPRSTW") for _ in range(3))}]
                if is_gc:
                    props = [{"name": "Recipient email", "value": "redacted"}]
                yield [ln["id"], o["id"], idx + 1, p["id"] if p else None, v["id"] if v else None,
                       p["title"] if p else ln["custom"], v["title"] if v else None, v["sku"] if v else None,
                       p["vendor"] if p else None, ln["qty"], money(ln["price"]), money(ln["discount"]),
                       boolstr(not is_gc and p is not None), boolstr(is_gc),
                       boolstr(p is not None and not is_gc),
                       None if o["fulfillment"] is None else "fulfilled",
                       jdump(props) if props else None, synced(o["updated_at"])]

    counts["raw_shopify.order_lines"] = write(
        "raw_shopify.order_lines",
        ["id", "order_id", "index", "product_id", "variant_id", "title", "variant_title", "sku", "vendor",
         "quantity", "price", "total_discount", "taxable", "gift_card", "requires_shipping", "fulfillment_status",
         "properties", "_fivetran_synced"], line_rows())

    counts["raw_shopify.refunds"] = write(
        "raw_shopify.refunds",
        ["id", "order_id", "created_at", "processed_at", "note", "restock", "refund_line_items",
         "_fivetran_synced"],
        ([r["id"], r["order"]["id"], ts_local(r["at"]), ts_local(r["at"] + timedelta(seconds=rng.randint(1, 30))),
          r["note"], boolstr(r["restock"]), jdump(r["lines"]), synced(r["at"])]
         for r in sorted(refunds, key=lambda r: r["at"])))

    counts["raw_shopify.transactions"] = write(
        "raw_shopify.transactions",
        ["id", "order_id", "refund_id", "parent_id", "kind", "status", "gateway", "amount", "currency",
         "authorization", "error_code", "test", "created_at", "processed_at", "receipt", "_fivetran_synced"],
        ([t["id"], t["order_id"], t["refund_id"], t["parent_id"], t["kind"], t["status"], t["gateway"],
          money(t["amount"]), t["currency"], t["authorization"], t["error_code"], boolstr(t["test"]),
          ts_local(t["created_at"]), ts_local(t["processed_at"]),
          jdump({"paid_amount": money(t["amount"]), "presentment_currency": t["presentment_currency"]}
                if t["status"] == "success" else {}), synced(t["created_at"])]
         for t in sorted(transactions, key=lambda t: t["created_at"])))

    # ---- raw_stripe
    brands = [("visa", 52), ("mastercard", 27), ("amex", 15), ("discover", 4), ("jcb", 1), ("unionpay", 1)]

    refunded_by_charge: dict[str, int] = {}
    for r in stripe_refunds:
        refunded_by_charge[r["charge"]["id"]] = refunded_by_charge.get(r["charge"]["id"], 0) + r["amount"]

    def charge_rows():
        for ch in sorted(stripe_charges, key=lambda c: c["created"]):
            o = ch["order"]
            refunded = refunded_by_charge.get(ch["id"], 0)
            if ch.get("voided_at"):
                refunded = ch["amount"]
            meta = {"shopify_order_id": str(o["id"]), "shopify_order_name": f"#{o['number']}"} if o else {
                "invoice": ch["wholesale"], "channel": "wholesale"}
            pmd = {"type": "card", "card": {"brand": pick(brands), "last4": f"{rng.randint(0, 9999):04d}",
                                            "country": o["country"] if o else "US",
                                            "funding": pick([("credit", 70), ("debit", 28), ("prepaid", 2)])}}
            yield [ch["id"], ch["amount"], ch["amount"] if ch["captured"] else 0, refunded, "usd", ch["status"],
                   boolstr(ch["status"] == "succeeded"), boolstr(ch["captured"]),
                   boolstr(refunded > 0 and refunded >= ch["amount"]),
                   f"cus_{o['account']['id'] % 10**10:010d}" if o and o["account"] is not None else None,
                   f"Shopify order #{o['number']}" if o else f"Wholesale invoice {ch['wholesale']}",
                   ch["failure"], {"card_declined": "Your card was declined."}.get(ch["failure"]),
                   ch["fee"], ch["payout"]["id"] if ch["payout"] else None, jdump(meta), jdump(pmd),
                   ts_utc(ch["created"]), ts_utc(ch["captured_at"]) if ch["captured_at"] else None,
                   ts_utc(min(CUTOFF, (ch["captured_at"] or ch["created"]) + timedelta(minutes=30)))]

    counts["raw_stripe.charges"] = write(
        "raw_stripe.charges",
        ["id", "amount", "amount_captured", "amount_refunded", "currency", "status", "paid", "captured",
         "refunded", "customer_id", "description", "failure_code", "failure_message", "fee", "payout_id",
         "metadata", "payment_method_details", "created", "captured_at", "_fivetran_synced"], charge_rows())

    counts["raw_stripe.refunds"] = write(
        "raw_stripe.refunds",
        ["id", "charge_id", "amount", "currency", "reason", "status", "payout_id", "metadata", "created",
         "_fivetran_synced"],
        ([r["id"], r["charge"]["id"], r["amount"], "usd", r["reason"], "succeeded", r["payout"]["id"],
          jdump({"shopify_refund_id": str(r["shopify_refund_id"])}),
          ts_utc(r["created"]), ts_utc(min(CUTOFF, r["created"] + timedelta(minutes=20)))]
         for r in sorted(stripe_refunds, key=lambda r: r["created"])))

    cutoff_date = CUTOFF.date()
    counts["raw_stripe.payouts"] = write(
        "raw_stripe.payouts",
        ["id", "amount", "currency", "arrival_date", "created", "status", "method", "type", "description",
         "_fivetran_synced"],
        ([po["id"], po["amount"], "usd", po["arrival"].isoformat(),
          ts_utc(datetime(po["arrival"].year, po["arrival"].month, po["arrival"].day, 2, 0, tzinfo=UTC)
                 - timedelta(days=2)),
          "paid" if po["arrival"] <= cutoff_date else "in_transit", "standard", "bank_account", "STRIPE PAYOUT",
          ts_utc(min(CUTOFF, datetime(po["arrival"].year, po["arrival"].month, po["arrival"].day, 12, 0, tzinfo=UTC)))]
         for po in sorted(payouts.values(), key=lambda p: p["arrival"])))

    # ---- raw_marketing
    counts["raw_marketing.ad_spend"] = write(
        "raw_marketing.ad_spend",
        ["date", "channel", "account_id", "campaign_id", "campaign_name", "impressions", "clicks", "spend",
         "currency", "platform_conversions", "platform_conversion_value", "_loaded_at"],
        ([r["date"].isoformat(), r["channel"], r["account_id"], r["campaign_id"], r["campaign_name"],
          r["impressions"], r["clicks"], money(r["spend"]), r["currency"], r["conversions"],
          r["conversion_value"], ts_utc(r["loaded_at"])] for r in ad_spend))

    counts["raw_marketing.web_sessions"] = write(
        "raw_marketing.web_sessions",
        ["session_id", "anonymous_id", "customer_id", "started_at", "duration_seconds", "landing_page",
         "referrer", "utm_source", "utm_medium", "utm_campaign", "device_type", "country_code", "pageviews",
         "converted_order_id", "_loaded_at"],
        ([s["session_id"], s["anonymous_id"], s["customer_id"], ts_utc(s["started_at"]), s["duration"],
          s["landing_page"], s["referrer"], s["utm_source"], s["utm_medium"], s["utm_campaign"],
          s["device_type"], s["country"], s["pageviews"], s["converted_order_id"],
          ts_utc(min(CUTOFF, s["started_at"] + timedelta(hours=rng.randint(1, 30))))] for s in sessions))

    # ---- oracle
    live = [o for o in orders if not o["test"] and not o["deleted"]]
    valid = [o for o in live if is_valid(o)]
    people = {}
    for c in customers:
        if c["kind"] == "staff" or c["deleted"]:
            continue
        key = ("id", c["id"]) if c["redacted"] else ("email", c["email"].strip().lower())
        people.setdefault(key, c)
    expected = {
        "seed": SEED,
        "raw_row_counts": counts,
        "stg_orders": len(live),
        "valid_orders": len(valid),
        "valid_net_revenue": money(sum(net_revenue(o) for o in valid)),
        "all_orders_net_revenue": money(sum(net_revenue(o) for o in live)),
        "valid_units_sold": sum(ln["qty"] for o in valid for ln in o["lines"]),
        "valid_line_count": sum(len(o["lines"]) for o in valid),
        "stg_customers": len(people),
        "stg_order_lines": sum(len(o["lines"]) for o in live),
        "black_friday_2025_valid_orders": sum(1 for o in valid if o["date"] == thanksgiving(2025) + timedelta(days=1)),
    }
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf8")
    print(json.dumps(expected, indent=2))


if __name__ == "__main__":
    main()
