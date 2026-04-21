# MTL Parking Tickets

An interactive map of Montreal parking tickets scraped from the city's official payment API. Visualizes ticket hotspots, offence types, fine amounts, and dates across the island.

---

## What it does

- **Scans** the Montreal ticket payment API using a discovered sequential ID pattern
- **Stores** all found tickets in a local SQLite database
- **Geocodes** ticket locations (intersections + street addresses) via Nominatim + Photon fallback
- **Renders** an interactive Apple MapKit map with color-coded hotspot dots, filters, and a detail panel per location

---

## Stack

| Layer | Tech |
|---|---|
| Scanner | Python — `requests`, `selenium` |
| Auth | Selenium → Chrome CDP → JWT intercept |
| Storage | SQLite (`tickets.db`) |
| Geocoding | Nominatim (OSM) + Photon fallback |
| Map | Apple MapKit JS |

No backend server. No Docker. Just Python scripts and a static HTML file.

---

## Setup

### Requirements

```bash
pip install requests selenium webdriver-manager
```

Chrome must be installed. `webdriver-manager` handles ChromeDriver automatically.

### Run the scanner

```bash
python scanner_v2.py
```

This will:
1. Open a Chrome window to intercept the JWT token from Montreal's site
2. Scan ticket IDs around the configured anchor point
3. Save hits to `tickets.db`
4. Geocode new addresses and write `map_tickets.json`

### View the map

Serve the directory locally (required — MapKit won't load from `file://`):

```bash
python -m http.server 8000
```

Then open [http://localhost:8000/map.html](http://localhost:8000/map.html)

---

## Commands

```bash
python scanner_v2.py              # full scan
python scanner_v2.py stats        # print DB stats
python scanner_v2.py map          # re-geocode + rebuild map_tickets.json
python scanner_v2.py test 918432023  # test a single ticket ID
python get_token.py               # extract JWT token only (headless)
python get_token.py --visible     # extract JWT token with visible browser
```

---

## Scanner config

Edit the constants at the top of `scanner_v2.py`:

```python
ANCHOR        = 918_431_345   # known ticket ID near current date
STEPS_BACK    = 7_000         # how far back to scan
STEPS_FORWARD = 1_000         # how far forward to scan
DELAY         = 0.2           # seconds between requests
```

To scan further back in time, increase `STEPS_BACK` or find an older anchor ticket with `test` mode and update `ANCHOR`.

---

## Ticket ID pattern

Montreal ticket IDs follow a predictable sequence:

```
+11 always
+4  if the last digit is 6
```

Valid IDs never end in 7, 8, or 9. The scanner generates the full valid window from the anchor and skips anything already in the database.

---

## Map features

- **Color by fine amount** — green (low) → orange (mid) → red (high)
- **Circle size** scales with ticket count at that location
- **Pulse ring** on hotspots (4+ tickets)
- **Count label** inside circles with multiple tickets
- **Detail panel** — click any dot to see all tickets: date, offence code + meaning, vehicle make, fine
- **Filters** — by date range, quick ranges (7d / 14d / month), offence code
- **Top hotspots** sidebar with one-click map fly-to

---

## Files

```
scanner_v2.py      — main scanner + geocoder + map exporter
get_token.py       — JWT token extractor via Selenium
map.html           — interactive map (static, no build step)
tickets.db         — SQLite database (gitignored)
map_tickets.json   — geocoded export for the map (gitignored)
geocache.json      — address → lat/lng cache (gitignored)
```

---

## Notes

- The JWT token expires after ~60 min. The scanner refreshes it automatically every 800 requests.
- Nominatim has a 1 req/sec rate limit. The geocoder respects this with sleeps and retries. If it fails, Photon (Komoot) is used as a fallback.
- French street name quirks are handled: `RUE NOUE DE` → `Rue de Noue`, intersections (`RUE X / RUE Y`) → `Street X and Street Y` for Nominatim.
- The `scanned` table tracks every ID checked (hit or not), so re-running the scanner never duplicates work.
