"""
Montreal Parking Ticket Scanner v2
====================================
Uses the official API:
  1. Selenium extracts JWT token from authorize call
  2. GET https://api.montreal.ca/api/justice/ticket/payment/v1/statements/{id}
     with Authorization: Bearer {token} -> get ticket JSON

Pattern: +11 always, +4 if last digit is 6
Anchor : 918,431,345 (April 9, 2026)

Usage:
  python scanner_v2.py          # full scan
  python scanner_v2.py test 918432023  # test single ticket
  python scanner_v2.py stats    # show DB stats
  python scanner_v2.py map      # export map JSON
"""

import requests
import sqlite3
import time
import json
import sys
from datetime import datetime
from collections import defaultdict
from get_token import fetch_token

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
STATEMENT_URL = "https://api.montreal.ca/api/justice/ticket/payment/v1/statements/{id}?limit=45&offset=0"
SITE_URL      = "https://services.montreal.ca/constats/paiement/recherche-constat"

DB_PATH  = "tickets.db"
MAP_PATH = "map_tickets.json"

ANCHOR        = 918_431_345
STEPS_BACK    = 7_000
STEPS_FORWARD = 5_000   # larger forward window to catch new tickets each day
DELAY         = 0.2

# Re-check NOT_FOUND tickets younger than this many days (city adds tickets with delay)
RECHECK_DAYS = 14

# Supabase — reads from environment variables (set as GitHub Actions secrets)
import os as _os
SUPABASE_URL = _os.getenv("SUPABASE_URL", "https://gkitztfupqxuhskvxtzw.supabase.co")
SUPABASE_KEY = _os.getenv("SUPABASE_KEY", "")

# Headless mode: always True in CI, False locally so you can see the browser
HEADLESS = _os.getenv("CI", "false").lower() in ("true", "1")

# Refresh token every N requests (token expires ~60 min)
TOKEN_REFRESH_EVERY = 800

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Origin":       "https://services.montreal.ca",
    "Referer":      SITE_URL,
    "Content-Type": "application/json",
    "Accept":       "application/json",
}


# ─────────────────────────────────────────────
# PATTERN
# ─────────────────────────────────────────────
def next_ticket(n):
    return n + 4 if (n % 10) == 6 else n + 11

def prev_ticket(n):
    return n - 4 if (n % 10) == 0 else n - 11

def generate_window(anchor, steps_back, steps_forward):
    low = anchor
    for _ in range(steps_back):
        low = prev_ticket(low)
    high = anchor
    for _ in range(steps_forward):
        high = next_ticket(high)
    return [n for n in range(low, high + 1) if (n % 10) not in (7, 8, 9)]


# ─────────────────────────────────────────────
# FETCH ONE TICKET
# ─────────────────────────────────────────────
def fetch_ticket(session, ticket_id, token):
    url = STATEMENT_URL.format(id=ticket_id)
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    try:
        r = session.get(url, headers=auth_headers, timeout=10)
        if r.status_code in (200, 304):
            data  = r.json()
            items = data.get("items", [])
            if items:
                item = items[0]
                return ("HIT", {
                    "number":       str(ticket_id),
                    "datetime":     item.get("offenceDate") or "",
                    "location":     item.get("offenceAddress") or "",
                    "offence_code": item.get("offenceCode") or "",
                    "amount":       item.get("dueAmount") or 0,
                    "vehicle_make": item.get("vehicleBrand") or "",
                    "is_payable":   item.get("isPayable") or False,
                    "status":       item.get("status") or "",
                })
            return ("NOT_FOUND", None)
        elif r.status_code == 404:
            return ("NOT_FOUND", None)
        elif r.status_code == 401:
            return ("AUTH_EXPIRED", None)
        elif r.status_code == 403:
            return ("TOO_RECENT", None)
        else:
            return ("ERROR", None)
    except KeyboardInterrupt:
        raise
    except:
        return ("ERROR", None)


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number   TEXT UNIQUE,
            datetime_issued TEXT,
            location        TEXT,
            offence_code    TEXT,
            amount          REAL,
            vehicle_make    TEXT,
            is_payable      INTEGER,
            scraped_at      TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scanned (
            ticket_number TEXT PRIMARY KEY,
            result        TEXT,
            scanned_at    TEXT
        )
    """)
    conn.commit()
    return conn

def load_known_numbers(conn):
    """Load all scanned + saved ticket numbers into a set in one query."""
    c = conn.cursor()
    c.execute("SELECT ticket_number FROM scanned")
    known = {row[0] for row in c.fetchall()}
    c.execute("SELECT ticket_number FROM tickets")
    known.update(row[0] for row in c.fetchall())
    return known

def purge_recent_not_found(conn, days=RECHECK_DAYS):
    """Remove NOT_FOUND entries scanned within the last N days so they get rechecked.
    The city adds tickets to their system with a delay, so a miss today may hit tomorrow."""
    cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).isoformat()
    c = conn.cursor()
    c.execute(
        "DELETE FROM scanned WHERE result='NOT_FOUND' AND scanned_at >= ?",
        (cutoff,)
    )
    conn.commit()
    removed = c.rowcount
    if removed:
        print(f"  Cleared {removed:,} recent NOT_FOUND entries for recheck (last {days} days)")

def upload_to_supabase(tickets):
    """Upload a list of new ticket dicts to Supabase. Skips if no key configured."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    url     = f"{SUPABASE_URL}/rest/v1/tickets"
    records = [
        {
            "ticket_number":   t["number"],
            "datetime_issued": t["datetime"] or None,
            "location":        t["location"] or None,
            "offence_code":    t["offence_code"] or None,
            "amount":          t["amount"],
            "vehicle_make":    t["vehicle_make"] or None,
            "is_payable":      bool(t.get("is_payable")),
            "scraped_at":      datetime.now().isoformat(),
        }
        for t in tickets
    ]
    r = requests.post(url, json=records, headers=headers)
    if r.status_code in (200, 201):
        print(f"  Supabase: {len(records)} ticket(s) uploaded")
    else:
        print(f"  Supabase upload error: {r.status_code} — {r.text[:120]}")

# Pending inserts buffered here; flushed every COMMIT_EVERY rows.
_scanned_buf = []
COMMIT_EVERY = 50

def mark_scanned(conn, n, result, known_set):
    known_set.add(str(n))
    _scanned_buf.append((str(n), result, datetime.now().isoformat()))
    if len(_scanned_buf) >= COMMIT_EVERY:
        _flush_scanned(conn)

def _flush_scanned(conn):
    if not _scanned_buf:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO scanned (ticket_number, result, scanned_at) VALUES (?, ?, ?)",
        _scanned_buf,
    )
    conn.commit()
    _scanned_buf.clear()

def save_ticket(conn, ticket, known_set):
    try:
        conn.execute("""
            INSERT OR IGNORE INTO tickets
            (ticket_number, datetime_issued, location, offence_code, amount, vehicle_make, is_payable, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticket["number"],
            ticket["datetime"],
            ticket["location"],
            ticket["offence_code"],
            ticket["amount"],
            ticket["vehicle_make"],
            1 if ticket.get("is_payable") else 0,
            datetime.now().isoformat()
        ))
        conn.commit()
        inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
        if inserted:
            known_set.add(ticket["number"])
        return inserted
    except Exception as e:
        print(f"  DB error: {e}")
        return False


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────
def show_stats(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tickets")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM scanned")
    scanned = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM scanned WHERE result='HIT'")
    hits = c.fetchone()[0]
    c.execute("SELECT location, COUNT(*) cnt FROM tickets GROUP BY location ORDER BY cnt DESC LIMIT 15")
    top_locs = c.fetchall()
    c.execute("SELECT offence_code, COUNT(*) cnt FROM tickets GROUP BY offence_code ORDER BY cnt DESC LIMIT 10")
    top_offences = c.fetchall()
    c.execute("SELECT SUBSTR(datetime_issued,1,10) day, COUNT(*) cnt FROM tickets GROUP BY day ORDER BY day DESC LIMIT 14")
    by_day = c.fetchall()

    print("\n" + "="*60)
    print("MONTREAL PARKING TICKET STATS")
    print("="*60)
    print(f"  Tickets collected : {total:,}")
    print(f"  Numbers scanned   : {scanned:,}")
    print(f"  Hit rate          : {hits/max(scanned,1)*100:.1f}%")
    print(f"\n  Top 15 hotspots:")
    for loc, cnt in top_locs:
        print(f"    {cnt:3}x  {loc}")
    print(f"\n  Top offence codes:")
    for code, cnt in top_offences:
        print(f"    {cnt:3}x  {code}")
    print(f"\n  By day:")
    for day, cnt in by_day:
        print(f"    {day}  ->  {cnt:,} tickets")


# ─────────────────────────────────────────────
# MAP EXPORT  (geocodes in Python, instant in browser)
# ─────────────────────────────────────────────
GEO_CACHE_PATH = "geocache.json"

def _load_geo_cache():
    try:
        with open(GEO_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def _save_geo_cache(cache):
    with open(GEO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

_ABBREV = [
    (r'\bAVEN\b',   'Avenue'),
    (r'\bBOUL\b',   'Boulevard'),
    (r'\bPL\b',     'Place'),
    (r'\bSQ\b',     'Square'),
    (r'\bCH\b',     'Chemin'),
    (r'\bCOTE\b',   'Côte'),
    (r'\bMTE\b',    'Montée'),
    (r'\bRTE\b',    'Route'),
    (r'\bST\b',     'Saint'),
    (r'\bSTE\b',    'Sainte'),
    (r'\bE\b',      'Est'),
    (r'\bO\b',      'Ouest'),
    (r'\bN\b',      'Nord'),
    (r'\bS\b',      'Sud'),
]

import re as _re

# Articles that Montreal's ticket system appends to the end of street names
# e.g. "RUE NOUE DE" → "Rue de la Noue", "RUE MAITRE LE" → "Rue le Maître"
_TRAILING_ARTICLES = _re.compile(
    r'^(.*?)\s+(DE LA|DE L|DU|DES|DE|LE|LA|LES|L)\s*$', _re.IGNORECASE
)

def _fix_trailing_article(name):
    """Move a trailing French article back to its natural position."""
    m = _TRAILING_ARTICLES.match(name.strip())
    if m:
        body, article = m.group(1), m.group(2)
        return f"{article} {body}"
    return name

def _normalize(part):
    """Normalize a single address part (no slash handling)."""
    if not part:
        return ""
    addr = part.strip()
    addr = _fix_trailing_article(addr)
    for pattern, replacement in _ABBREV:
        addr = _re.sub(pattern, replacement, addr, flags=_re.IGNORECASE)
    return addr.title()

def _build_attempts(address):
    """Return ordered list of query strings to try, handling intersections."""
    city_variants = [
        ", Montréal, QC, Canada",
        ", Montreal, Quebec, Canada",
        ", Montréal, Canada",
    ]

    if "/" in address:
        parts = [p.strip() for p in address.split("/", 1)]
        norm  = [_normalize(p) for p in parts]
        attempts = []
        for sfx in city_variants:
            attempts.append(f"{norm[0]} and {norm[1]}{sfx}")
            attempts.append(f"{norm[0]} & {norm[1]}{sfx}")
        # Fallback: just the first street
        for sfx in city_variants:
            attempts.append(f"{norm[0]}{sfx}")
        return attempts

    norm = _normalize(address)
    raw  = address.title()
    attempts = []
    for sfx in city_variants:
        attempts.append(f"{norm}{sfx}")
    attempts.append(f"{raw}, Montréal, QC, Canada")
    return attempts

def _nominatim(session, query):
    r = session.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "mtl-tickets-map/1.0 (research)", "Accept-Language": "fr,en"},
        timeout=15,
    )
    if not r.text or not r.text.strip():
        return None  # rate-limited / empty body
    results = r.json()
    if results:
        return {"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])}
    return None

def _photon(session, query):
    """Komoot Photon — OSM-backed, no strict rate limit, good French support."""
    r = session.get(
        "https://photon.komoot.io/api/",
        params={"q": query, "limit": 1, "lang": "fr", "bbox": "-73.98,45.40,-73.47,45.70"},
        headers={"User-Agent": "mtl-tickets-map/1.0"},
        timeout=15,
    )
    if not r.text or not r.text.strip():
        return None
    data = r.json()
    features = data.get("features", [])
    if features:
        coords = features[0]["geometry"]["coordinates"]
        return {"lat": coords[1], "lng": coords[0]}
    return None

def geocode_address(address, cache, session):
    if address in cache:
        return cache[address]

    attempts = _build_attempts(address)

    # Try Nominatim first with retries + backoff
    for query in attempts:
        for wait in (1.2, 2.5, 5.0):
            try:
                result = _nominatim(session, query)
                if result:
                    cache[address] = result
                    return result
                time.sleep(wait)
                break  # empty result (not an error) — move to next query
            except Exception:
                time.sleep(wait)  # network hiccup — retry same query

    # Fallback: Photon geocoder
    for query in attempts[:3]:
        try:
            result = _photon(session, query)
            if result:
                cache[address] = result
                return result
            time.sleep(1.0)
        except Exception:
            time.sleep(1.0)

    return None

def export_map(conn):
    c = conn.cursor()
    c.execute("""
        SELECT ticket_number, datetime_issued, location, offence_code, amount, vehicle_make
        FROM tickets ORDER BY datetime_issued DESC
    """)
    rows = c.fetchall()

    # Group by location
    locs = defaultdict(lambda: {"count": 0, "tickets": []})
    for row in rows:
        loc = row[2]
        if not loc:
            continue
        locs[loc]["count"] += 1
        locs[loc]["tickets"].append({
            "number":   row[0],
            "datetime": row[1],
            "offence":  row[3],
            "amount":   row[4],
            "make":     row[5],
        })

    # Geocode all unique addresses (cached — only new/failed ones hit the API)
    cache   = _load_geo_cache()
    # Remove previously failed entries so they get retried with better normalisation
    cache   = {k: v for k, v in cache.items() if v is not None}
    session = requests.Session()
    addrs   = list(locs.keys())
    need    = [a for a in addrs if a not in cache]
    def _write_map_json():
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
        placed_tickets = sum(l["count"] for l in locations)
        data = {
            "total":     placed_tickets,
            "total_db":  len(rows),
            "generated": datetime.now().isoformat(),
            "locations": locations,
        }
        with open(MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return len(locations)

    print(f"\nGeocoding: {len(need)} new addresses ({len(addrs)-len(need)} cached)…")
    if not need:
        _write_map_json()
    for i, addr in enumerate(need):
        result = geocode_address(addr, cache, session)
        status = f"{result['lat']:.4f},{result['lng']:.4f}" if result else "FAILED"
        print(f"  [{i+1}/{len(need)}] {addr} → {status}")
        if (i + 1) % 10 == 0 or i == len(need) - 1:
            _save_geo_cache(cache)
            placed = _write_map_json()
            print(f"  → map_tickets.json updated ({placed} locations placed)")
        time.sleep(1.1)

    placed = _write_map_json()
    skipped = len(locs) - placed
    print(f"\nMap exported: {len(rows):,} tickets across {placed:,} locations", end="")
    if skipped:
        print(f" ({skipped} skipped — no geocode)", end="")
    print()


# ─────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────
def run_scanner():
    print("Montreal Parking Ticket Scanner v2")
    print("="*60)
    print(f"Anchor  : {ANCHOR:,}")
    print(f"Window  : {STEPS_BACK:,} back, {STEPS_FORWARD:,} forward")
    print(f"DB      : {DB_PATH}\n")

    conn    = init_db()
    session = requests.Session()

    # Get token via Selenium
    print("Getting auth token via Selenium...")
    token = fetch_token(headless=HEADLESS)
    if not token:
        print("ERROR: Could not get token. Try running: python get_token.py --visible")
        return
    print(f"Token OK: {token[:40]}...\n")

    # Re-open recently-missed IDs so the city's delayed entries get picked up
    purge_recent_not_found(conn)

    # Generate window — skip anything already in scanned or tickets tables
    print("Generating scan window...")
    known   = load_known_numbers(conn)
    window  = generate_window(ANCHOR, STEPS_BACK, STEPS_FORWARD)
    to_scan = [n for n in window if str(n) not in known]
    print(f"  Valid numbers : {len(window):,}")
    print(f"  Already known : {len(window) - len(to_scan):,}")
    print(f"  To scan       : {len(to_scan):,}")
    print(f"  Est. time     : ~{len(to_scan) * DELAY / 60:.0f} minutes\n")

    hits = not_found = errors = new_hits = 0
    new_tickets_buf = []  # buffer for Supabase upload
    GEO_FLUSH_EVERY = 10

    # Load geocache upfront so we geocode inline and never redo cached addresses
    geo_cache = _load_geo_cache()
    geo_cache  = {k: v for k, v in geo_cache.items() if v is not None}
    geo_session = requests.Session()

    for i, n in enumerate(to_scan):
        try:
            # Refresh token periodically
            if i > 0 and i % TOKEN_REFRESH_EVERY == 0:
                print(f"\n  Refreshing token at step {i:,}...")
                new_token = fetch_token(headless=HEADLESS)
                if new_token:
                    token = new_token
                    print(f"  Token refreshed OK\n")
                else:
                    print(f"  Token refresh failed, continuing with old token\n")

            status, ticket = fetch_ticket(session, n, token)
            mark_scanned(conn, n, status, known)

            if status == "HIT":
                if ticket is None:
                    continue
                hits += 1
                is_new = save_ticket(conn, ticket, known)
                marker = "NEW" if is_new else "DUP"
                print(f"  [{marker}] {n:,} | {ticket['datetime'][:16]:16} | ${ticket['amount']:6.0f} | {ticket['location'][:35]}")

                # Geocode inline — skip if address already cached
                if is_new:
                    new_tickets_buf.append(ticket)
                    new_hits += 1
                    addr = ticket.get("location", "")
                    if addr and addr not in geo_cache:
                        result = geocode_address(addr, geo_cache, geo_session)
                        geo_status = f"{result['lat']:.4f},{result['lng']:.4f}" if result else "FAILED"
                        print(f"    geo: {addr[:40]} → {geo_status}")
                        time.sleep(1.1)  # Nominatim rate limit
                    if new_hits % GEO_FLUSH_EVERY == 0:
                        _save_geo_cache(geo_cache)
                        export_map(conn)
                        print(f"  [map updated — {new_hits} new hits so far]")

            elif status == "NOT_FOUND":
                not_found += 1

            elif status == "AUTH_EXPIRED":
                print(f"\n  Token expired at {n:,}, refreshing...")
                new_token = fetch_token(headless=HEADLESS)
                if new_token:
                    token = new_token
                    print(f"  Token refreshed OK\n")

            elif status == "ERROR":
                errors += 1
                if errors % 20 == 0:
                    print(f"  {errors} errors so far at {n:,}...")
                time.sleep(1)

            if (i + 1) % 500 == 0:
                pct = (i+1)/len(to_scan)*100
                print(f"\n  [{pct:.0f}%] {i+1:,}/{len(to_scan):,} | Hits:{hits:,} | Not found:{not_found:,} | Errors:{errors:,}\n")

            time.sleep(DELAY)

        except KeyboardInterrupt:
            print(f"\nStopped at {n:,}")
            break

    _flush_scanned(conn)  # commit any remaining buffered rows
    _save_geo_cache(geo_cache)
    if new_tickets_buf:
        upload_to_supabase(new_tickets_buf)
    print(f"\nDONE — Hits:{hits:,} | Not found:{not_found:,} | Errors:{errors:,}")
    show_stats(conn)
    export_map(conn)
    conn.close()


# ─────────────────────────────────────────────
# TEST MODE
# ─────────────────────────────────────────────
def test_single(ticket_num):
    print(f"Testing ticket {ticket_num:,}...")
    session = requests.Session()

    print("Getting token via Selenium...")
    token = fetch_token(headless=HEADLESS)
    if not token:
        print("ERROR: Could not get token")
        return

    print(f"Token OK: {token[:40]}...")
    status, ticket = fetch_ticket(session, ticket_num, token)
    print(f"\nStatus: {status}")
    if ticket:
        print(f"  Number   : {ticket['number']}")
        print(f"  DateTime : {ticket['datetime']}")
        print(f"  Location : {ticket['location']}")
        print(f"  Offence  : {ticket['offence_code']}")
        print(f"  Amount   : ${ticket['amount']}")
        print(f"  Make     : {ticket['vehicle_make']}")
        print(f"  Payable  : {ticket['is_payable']}")
    else:
        print("  No data returned")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        conn = init_db()
        show_stats(conn)
        conn.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "map":
        conn = init_db()
        export_map(conn)
        conn.close()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        num = int(sys.argv[2]) if len(sys.argv) > 2 else 918432023
        test_single(num)
    else:
        run_scanner()