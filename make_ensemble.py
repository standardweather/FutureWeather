"""Pack the latest NOAA GEFS ensemble into small binary tiles for FutureWeather's forecast confidence view.

GEFS (Global Ensemble Forecast System) is the GFS run 31 times: a control and 30 members started from slightly
different conditions. Where they agree the forecast is more certain; where they spread out it isn't. This reads
each member's 6-hourly maximum and minimum temperature, 10 m wind and precipitation for F006–F168 from the
0.5° files in NOAA's open-data bucket on AWS (only those messages, using the .idx byte offsets) and writes:

  site/ens/index.json           run info and the tile layout
  site/ens/<row>_<col>.bin      one tile of 10 × 10 grid points (5° × 5°)

Each point is a block of M members × S steps per variable: max temperature, min temperature and wind as
unsigned bytes (°F + 80, mph; 255 missing), then precipitation as little-endian uint16 (hundredths of an
inch; 65535 missing). Points are stored row by row from the northwest corner of the tile.

    python make_ensemble.py          # FW_HOURS=2 and FW_MEMBERS=3 shorten it for local testing
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np

from make_plots import OUT, byte_range, decode, latest_cycle, read_index, session

BUCKET = "https://noaa-gefs-pds.s3.amazonaws.com"
MEMBERS = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]
HOURS = list(range(6, 169, 6))
# 0.5° points over the lower 48: 50°N to 24°N, 125°W to 66°W.
NORTH, SOUTH, WEST, EAST, RES = 50.0, 24.0, -125.0, -66.0, 0.5
NLAT, NLON = round((NORTH - SOUTH) / RES) + 1, round((EAST - WEST) / RES) + 1
TILE = 10
FIELDS = {
    "tmax": "TMAX:2 m above ground",
    "tmin": "TMIN:2 m above ground",
    "u": "UGRD:10 m above ground",
    "v": "VGRD:10 m above ground",
    "apcp": "APCP:surface",
}
SUBSET = (slice(NORTH, SOUTH), slice(WEST % 360, EAST % 360))


def url(cycle, member, fhr):
    return f"{BUCKET}/gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos/pgrb2ap5/{member}.t{cycle:%H}z.pgrb2a.0p50.f{fhr:03d}"


def fetch(cycle, member, fhr):
    """Raw GRIB messages for one member and hour, or None when the file isn't there."""
    u = url(cycle, member, fhr)
    try:
        index = read_index(u)
        out = {}
        for name, match in FIELDS.items():
            start, end = byte_range(index, match, u)
            resp = session.get(u, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
            resp.raise_for_status()
            out[name] = resp.content
        return out
    except Exception as e:  # a missing member or hour leaves gaps, not a failed run
        print(f"  {member} F{fhr:03d} skipped: {e}")
        return None


def main():
    members = MEMBERS[: int(os.environ["FW_MEMBERS"])] if os.environ.get("FW_MEMBERS") else MEMBERS
    hours = HOURS[: int(os.environ["FW_HOURS"])] if os.environ.get("FW_HOURS") else HOURS
    cycle = latest_cycle(lambda c, h: url(c, MEMBERS[-1], h), HOURS[-1])
    print(f"GEFS: cycle {cycle:%Y-%m-%d %HZ}, {len(members)} members, {len(hours)} hours")
    M, S = len(members), len(hours)
    tmax = np.full((M, S, NLAT, NLON), np.nan, np.float32)
    tmin, wind, apcp = tmax.copy(), tmax.copy(), tmax.copy()
    jobs = [(m, s) for m in range(M) for s in range(S)]
    # Downloads in parallel; decoding on this thread, since ecCodes isn't guaranteed to be thread-safe.
    with ThreadPoolExecutor(max_workers=16) as pool:
        for (m, s), blobs in zip(jobs, pool.map(lambda j: fetch(cycle, members[j[0]], hours[j[1]]), jobs)):
            if not blobs:
                continue
            f = {k: decode(b, subset=SUBSET).values for k, b in blobs.items()}
            tmax[m, s] = (f["tmax"] - 273.15) * 9 / 5 + 32
            tmin[m, s] = (f["tmin"] - 273.15) * 9 / 5 + 32
            wind[m, s] = np.hypot(f["u"], f["v"]) * 2.23694
            apcp[m, s] = f["apcp"] / 25.4
            if s == S - 1:
                print(f"  {members[m]} done")

    def u8(a, offset=0):
        v = np.round(a + offset)
        return np.where(np.isfinite(v), np.clip(v, 0, 254), 255).astype(np.uint8)

    packed = [u8(tmax, 80), u8(tmin, 80), u8(wind)]
    rain = np.round(apcp * 100)
    rain = np.where(np.isfinite(rain), np.clip(rain, 0, 65534), 65535).astype("<u2")
    out = OUT / "ens"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.bin"):
        old.unlink()
    tiles = 0
    for tr in range(0, NLAT, TILE):
        for tc in range(0, NLON, TILE):
            chunks = []
            for r in range(tr, tr + TILE):
                for c in range(tc, tc + TILE):
                    if r < NLAT and c < NLON:
                        chunks += [p[:, :, r, c].tobytes() for p in packed] + [rain[:, :, r, c].tobytes()]
                    else:
                        chunks += [b"\xff" * (M * S)] * 3 + [b"\xff\xff" * (M * S)]
            (out / f"{tr // TILE}_{tc // TILE}.bin").write_bytes(b"".join(chunks))
            tiles += 1
    (out / "index.json").write_text(json.dumps({
        "model": "GEFS", "long": "NOAA Global Ensemble Forecast System (GEFS) 0.5°",
        "cycle": cycle.strftime("%Y-%m-%dT%HZ"), "init": cycle.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "members": members, "hours": hours,
        "grid": {"north": NORTH, "west": WEST, "res": RES, "nlat": NLAT, "nlon": NLON, "tile": TILE},
        "vars": ["tmax", "tmin", "wind", "apcp"],
    }, indent=1))
    print(f"wrote {tiles} tiles")


if __name__ == "__main__":
    main()
