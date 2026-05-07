"""
Rebuild map_tickets.json from Supabase DB and re-upload to Storage.
Run this when the local DB is out of sync with Supabase.

  python _rebuild_map.py
"""

import os, json, requests, time, re
from datetime import datetime
from collections import defaultdict

# Load .env
_env = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env):
    for _line in open(_env):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
MAP_PATH     = os.path.join(os.path.dirname(__file__), "map_tickets.json")
GEO_CACHE    = os.path.join(os.path.dirname(__file__), "geo_cache.json")

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}


def fetch_all_tickets():
    """Pull every row from Supabase tickets table with pagination."""
    tickets = []
    page_size = 1000
    offset = 0
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/tickets"
            f"?select=ticket_number,datetime_issued,location,offence_code,amount,vehicle_make,status"
            f"&order=datetime_issued.desc"
            f"&limit={page_size}&offset={offset}"
        )
        r = requests.get(url, headers=HEADERS)
        if r.status_code != 200:
            print(f"  Fetch error {r.status_code}: {r.text[:120]}")
            break
        batch = r.json()
        tickets.extend(batch)
        print(f"  Fetched {len(tickets)} tickets so far...")
        if len(batch) < page_size:
            break
        offset += page_size
    return tickets


def load_geo_cache():
    try:
        with open(GEO_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_geo_cache(cache):
    with open(GEO_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


_ABBREVS = [
    (r'\bAVEN\b',  'Avenue'),
    (r'\bBOUL\b',  'Boulevard'),
    (r'\bCHEM\b',  'Chemin'),
    (r'\bPLAC\b',  'Place'),
    (r'\bCOTE\b',  'Côte'),
    (r'\bST-\b',   'Saint-'),
    (r'\bSTE-\b',  'Sainte-'),
    (r'\s+O\b',    ' Ouest'),
    (r'\s+E\b',    ' Est'),
    (r'\s+N\b',    ' Nord'),
    (r'\s+S\b',    ' Sud'),
]

def _normalize(addr):
    # For intersections, just use the first street
    addr = addr.split("/")[0].strip()
    for pattern, replacement in _ABBREVS:
        addr = re.sub(pattern, replacement, addr, flags=re.IGNORECASE)
    return addr


def geocode(addr, cache, session):
    if addr in cache:
        return cache[addr]
    normalized = _normalize(addr)
    for query in [normalized, addr]:
        try:
            r = session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query + ", Montréal, QC, Canada", "format": "json", "limit": 1},
                headers={"User-Agent": "MTLParking/1.0"},
                timeout=10,
            )
            data = r.json()
            if data:
                result = {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
                cache[addr] = result
                return result
        except Exception:
            pass
    cache[addr] = None
    return None


def build_and_upload():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_KEY not set in .env")
        return

    print("Fetching all tickets from Supabase...")
    tickets = fetch_all_tickets()
    print(f"Total: {len(tickets)} tickets\n")

    # Group by location
    locs = defaultdict(lambda: {"count": 0, "tickets": []})
    for t in tickets:
        loc = t.get("location")
        if not loc:
            continue
        locs[loc]["count"] += 1
        entry = {
            "number":   t["ticket_number"],
            "datetime": t["datetime_issued"],
            "offence":  t["offence_code"],
            "amount":   t["amount"],
            "make":     t["vehicle_make"],
        }
        if t.get("status"):
            entry["status"] = t["status"]
        locs[loc]["tickets"].append(entry)

    cache   = {k: v for k, v in load_geo_cache().items() if v is not None}
    session = requests.Session()
    addrs   = list(locs.keys())
    need    = [a for a in addrs if a not in cache]
    print(f"Geocoding: {len(need)} new addresses ({len(addrs) - len(need)} cached)...")

    def _build_and_push(label=""):
        locations = []
        for addr, d in locs.items():
            geo = cache.get(addr)
            if not geo:
                continue
            locations.append({
                "address": addr,
                "lat":     geo["lat"],
                "lng":     geo["lng"],
                "count":   d["count"],
                "tickets": d["tickets"],
            })
        locations.sort(key=lambda x: x["count"], reverse=True)
        placed = sum(l["count"] for l in locations)
        data = {
            "total":     placed,
            "total_db":  len(tickets),
            "generated": datetime.now().isoformat(),
            "locations": locations,
        }
        with open(MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        raw = open(MAP_PATH, "rb").read()
        url = f"{SUPABASE_URL}/storage/v1/object/maps/map_tickets.json"
        r = requests.put(url, data=raw, headers={
            **HEADERS, "Content-Type": "application/json", "x-upsert": "true",
        })
        ok = r.status_code in (200, 201)
        print(f"  {'Uploaded' if ok else 'Upload FAILED'} {label}— {placed} tickets placed / {len(tickets)} total")
        return placed

    # Upload immediately with whatever is cached so far (might already have some)
    _build_and_push("(initial) ")

    for i, addr in enumerate(need):
        result = geocode(addr, cache, session)
        status = f"{result['lat']:.4f},{result['lng']:.4f}" if result else "FAILED"
        print(f"  [{i+1}/{len(need)}] {addr} -> {status}")
        # Save cache + push live update every 50 addresses
        if (i + 1) % 50 == 0 or i == len(need) - 1:
            save_geo_cache(cache)
            _build_and_push(f"[{i+1}/{len(need)}] ")
        time.sleep(1.1)

    save_geo_cache(cache)


if __name__ == "__main__":
    build_and_upload()
