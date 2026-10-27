"""Refresh site/stations.json, the airports used for forecast station plots on the Surface map.

    python make_stations.py

METAR sites inside the map bounds, from the Aviation Weather Center's station list. Each row is
[label, lat, lon, priority]; a lower priority is a bigger airport, which the page keeps when it thins the
plots to fit the zoom level. The list rarely changes, so it is committed rather than rebuilt every run.
"""

import gzip
import json
from pathlib import Path

import requests

from make_plots import BOUNDS   # the same map extent

URL = "https://aviationweather.gov/data/cache/stations.cache.json.gz"

rows = []
for s in json.loads(gzip.decompress(requests.get(URL, timeout=60).content)):
    if "METAR" not in (s.get("siteType") or []) or not s.get("icaoId"):
        continue
    if not (BOUNDS["south"] <= s["lat"] <= BOUNDS["north"] and BOUNDS["west"] <= s["lon"] <= BOUNDS["east"]):
        continue
    icao = s["icaoId"]
    # U.S. airports go by their three-letter code (BNA, not KBNA), as on NWS surface maps.
    label = icao[1:] if s.get("country") == "US" and len(icao) == 4 and icao[0] == "K" else icao
    rows.append([label, round(s["lat"], 3), round(s["lon"], 3), s["priority"] if s.get("priority") is not None else 9])
rows.sort(key=lambda r: (r[3], r[0]))
out = Path(__file__).parent / "site" / "stations.json"
out.write_text("[\n" + ",\n".join(json.dumps(r) for r in rows) + "\n]\n")
print(f"{len(rows)} stations -> {out}")
