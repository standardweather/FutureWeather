"""AIRMETcast: experimental AIRMET- and convective SIGMET-style hazard areas, worked out from raw model output.

This is not an official product. The unlisted AIRMETcast page that draws it lives in the AviationWeather repo
(/AviationWeather/airmetcast/) and reads these files from /FutureWeather/airmetcast/data/. It shares only the
download helpers with make_plots.py; every hazard is computed again from the raw GRIB fields.

    python make_airmetcast.py gfs         # Sierra, Tango and Zulu areas from the GFS column, F003–F048 every 3 h, then to F168 every 6 h (as FutureWeather's maps)
    python make_airmetcast.py hrrr        # IFR, mountain obscuration and convective areas from the HRRR, F01–F48 (its 00/06/12/18Z runs)
    python make_airmetcast.py calibrate   # fit turb_climate.json from past GFS runs (run once, then commit it)

Output, per model:
  site/airmetcast/data/<model>/f<hhh>.geojson   hazard polygons, lines and points for one forecast hour
  site/airmetcast/data/<model>.json             run info and the hours drawn

How each hazard is found (thresholds are starting points, to be tuned against PIREPs and archived G-AIRMETs):
  IFR             ceiling below 1,000 ft AGL or visibility below 3 SM over at least half of a ~60 km box
  MTN OBSCN       cloud base or ceiling at or below the local ridge tops, in mountainous terrain
  TURB-HI/-LO     GTG-style: Ellrod TI1/TI2, inverse Richardson number, frontogenesis and vertical shear per layer,
                  each mapped to an EDR scale with turb_climate.json, averaged, plus boundary-layer and mountain wave
                  terms; moderate is EDR >= 0.22. Split at FL180.
  SFC WND         sustained 10 m wind of 30 kt or more
  LLWS            30 kt or more of wind difference between 10 m and 2,000 ft AGL
  ICE             FIP-style: supercooled liquid (cloud + rain water) × temperature interest × cloud-top temperature
                  scenario × lift, per level; moderate where the score and liquid water are both high enough
  FZLVL, M_FZLVL  0 °C crossings in the raw temperature profile
  Convective      HRRR composite reflectivity >= 40 dBZ (or lightning) covering 40% of an area; AREA, LINE or ISOL,
                  with echo tops, motion from successive hours, and supercell, hail and wind tags
Areas smaller than MIN_AREA_SQMI (the 3,000 sq mi used for real AIRMETs and SIGMETs) are left out.
"""

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import contourpy
import eccodes
import numpy as np
import shapely
from scipy.ndimage import binary_dilation, gaussian_filter, label, map_coordinates, maximum_filter, minimum_filter, uniform_filter
from shapely.geometry import LineString, Polygon, mapping, shape

from make_plots import byte_range, latest_cycle, read_index, session

OUT = Path(__file__).parent / "site" / "airmetcast" / "data"
CLIMATE = Path(__file__).parent / "turb_climate.json"

MIN_AREA_SQMI = 3000

# Roughly the lower 48 and their coastal waters, where G-AIRMETs are issued. Shapes are clipped to it.
DOMAIN = Polygon([(-126, 49), (-95.2, 49), (-94.8, 49.4), (-89.5, 48), (-84.5, 46.6), (-82.4, 45.3), (-82.5, 42), (-79, 42.5),
                  (-79, 43.6), (-76.4, 44.2), (-74.7, 45), (-71.5, 45), (-70, 46.4), (-69.2, 47.5), (-67.8, 47.1), (-66.5, 45),
                  (-65, 43), (-66, 40), (-74, 35), (-75, 31), (-79, 25), (-80, 23.5), (-82, 23.5), (-84, 24.5), (-90, 25),
                  (-94, 25), (-97, 25.8), (-99.5, 26.8), (-101.4, 29.8), (-104.5, 29.5), (-106.5, 31.8), (-111, 31.3),
                  (-114.8, 32.5), (-117.1, 32.5), (-118, 31.5), (-121, 33), (-125, 39), (-126, 49)])
FT = 3.28084
KT = 1.94384
G, RD = 9.80665, 287.05

NOTE = ("Experimental, worked out automatically from raw model output. Not an AIRMET, SIGMET or any official "
        "product, and not for flight planning. Use aviationweather.gov and an official briefing.")


# ---------------------------------------------------------------- downloads and decoding

def fetch(url, wanted, workers=16):
    """{name: GRIB message bytes} for {name: index match}, by byte range. Missing fields are left out."""
    for i in range(4):
        try:
            index = read_index(url)
            break
        except Exception:
            if i == 3:
                raise
            time.sleep(2 ** i)

    def one(match):
        try:
            start, end = byte_range(index, match, url)
        except KeyError:
            return None
        for i in range(5):
            try:
                resp = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
                resp.raise_for_status()
                return resp.content
            except Exception:
                if i == 4:
                    raise
                time.sleep(2 ** i)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        blobs = dict(zip(wanted, pool.map(one, wanted.values())))
    return {k: v for k, v in blobs.items() if v is not None}


class Grid:
    """Where a model's (thinned or cut) grid sits: 2-D lat/lon, cell areas, and how to cut a message."""

    def __init__(self, lat, lon, cut, cell_km):
        self.lat, self.lon, self.cut, self.cell_km = lat, lon, cut, cell_km
        self.shape = lat.shape
        if cell_km is None:   # regular lat/lon: cells shrink toward the pole
            dlat = abs(lat[1, 0] - lat[0, 0])
            dlon = abs(lon[0, 1] - lon[0, 0])
            self.dy_m = dlat * 111195.0
            self.dx_m = dlon * 111195.0 * np.cos(np.radians(lat))
        else:
            self.dy_m = cell_km * 1000.0
            self.dx_m = np.full(lat.shape, cell_km * 1000.0)
        self.area_sqmi = self.dx_m * self.dy_m / 2.58999e6

    def km(self):
        return float(np.sqrt(np.mean(self.dx_m) * self.dy_m) / 1000)


def decode(blob, grid):
    h = eccodes.codes_new_from_message(blob)
    try:
        kind = eccodes.codes_get(h, "gridType")
        nx, ny = (eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")) if kind == "regular_ll" else \
                 (eccodes.codes_get(h, "Nx"), eccodes.codes_get(h, "Ny"))
        vals = eccodes.codes_get_values(h).astype(np.float32)
        if eccodes.codes_get(h, "bitmapPresent"):
            vals[vals == eccodes.codes_get(h, "missingValue")] = np.nan
    finally:
        eccodes.codes_release(h)
    return vals.reshape(ny, nx)[grid.cut]


def decode_all(blobs, grid):
    return {k: decode(b, grid) for k, b in blobs.items()}


# ---------------------------------------------------------------- GFS

GFS_BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150]
ICE_LEVELS = [p for p in LEVELS if p >= 300]
# 60–15°N, 135–55°W on the 0.25° grid (rows run north to south, longitudes 0–360).
GFS_CUT = (slice(120, 301), slice(900, 1221))


def gfs_url(cycle, fhr):
    return f"{GFS_BUCKET}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{fhr:03d}"


def gfs_grid():
    lat = 90 - 0.25 * np.arange(721)[GFS_CUT[0]]
    lon = 0.25 * np.arange(1440)[GFS_CUT[1]] - 360
    lon2, lat2 = np.meshgrid(lon, lat)
    return Grid(lat2, lon2, GFS_CUT, None)


def gfs_wanted(fhr, upper_only=False):
    w = {}
    for p in LEVELS:
        for var, key in (("UGRD", "u"), ("VGRD", "v"), ("TMP", "t"), ("HGT", "z")):
            w[f"{key}{p}"] = f"{var}:{p} mb:"
    w["psfc"] = "PRES:surface:"
    w["zsfc"] = "HGT:surface:"
    if upper_only:
        return w
    for p in LEVELS:
        w[f"rh{p}"] = f"RH:{p} mb:"
        w[f"w{p}"] = f"VVEL:{p} mb:"
    for p in ICE_LEVELS:
        for var, key in (("CLMR", "clw"), ("RWMR", "rw"), ("ICMR", "ci"), ("SNMR", "sn"), ("GRLE", "gr")):
            w[f"{key}{p}"] = f"{var}:{p} mb:"
    w.update({
        "hpbl": "HPBL:surface:", "vis": "VIS:surface:", "ceil": "HGT:cloud ceiling:",
        "u10": "UGRD:10 m above ground:", "v10": "VGRD:10 m above ground:",
        "t2m": "TMP:2 m above ground:", "d2m": "DPT:2 m above ground:",
        "gust": f"GUST:surface:{fhr} hour fcst", "prate": f"PRATE:surface:{fhr} hour fcst",
        "cfrzr": f"CFRZR:surface:{fhr} hour fcst",
    })
    return w


def column(f):
    """Stack pressure-level fields into (level, y, x) arrays, lowest level first, with below-ground levels masked."""
    c = {k: np.stack([f[f"{k}{p}"] for p in LEVELS]) for k in ("u", "v", "t", "z")}
    p = np.array(LEVELS, dtype=np.float32)[:, None, None]
    c["p"] = p
    c["above"] = (f["psfc"][None] / 100 >= p) & (c["z"] >= f["zsfc"][None])
    return c


def pressure_alt_ft(p_hpa):
    return 145366.45 * (1 - (np.asarray(p_hpa, dtype=float) / 1013.25) ** 0.190284)


def level_alt_ft(c):
    """Altitude of each level the way pilots read it: feet MSL below FL180, pressure altitude (flight level) above."""
    msl = c["z"] * FT
    pa = pressure_alt_ft(c["p"]) * np.ones_like(msl)
    return np.where(c["p"] <= 500, pa, msl)


# ---------------------------------------------------------------- turbulence

TURB_DIAGS = ["ti1", "ti2", "invri", "fgen", "vws"]
EDR_C1, EDR_C2 = -2.572, 0.5067   # climatological ln(EDR) mean and spread (Sharman & Pearson 2017)
EDR_MOD, EDR_SEV = 0.22, 0.34


def ddx(a, g):
    return np.gradient(a, axis=-1) / g.dx_m


def ddy(a, g):
    # Rows run north to south on the GFS grid, so a step down a row is a step south.
    return -np.gradient(a, axis=-2) / g.dy_m


def layer_diagnostics(c, g):
    """Raw turbulence diagnostics for each layer between adjacent levels: {name: (layer, y, x)}."""
    u, v, t, z, p = c["u"], c["v"], c["t"], c["z"], c["p"]
    theta = t * (1000.0 / p) ** 0.2857
    dz = np.maximum(z[1:] - z[:-1], 1.0)
    du, dv = u[1:] - u[:-1], v[1:] - v[:-1]
    vws = np.hypot(du, dv) / dz
    th_m = (theta[1:] + theta[:-1]) / 2
    n2 = G / th_m * (theta[1:] - theta[:-1]) / dz
    invri = np.clip(vws ** 2 / np.maximum(n2, 1e-5), 0, 100)
    um, vm = (u[1:] + u[:-1]) / 2, (v[1:] + v[:-1]) / 2
    ux, uy, vx, vy = ddx(um, g), ddy(um, g), ddx(vm, g), ddy(vm, g)
    deform = np.hypot(ux - vy, vx + uy)
    div = ux + vy
    tx, ty = ddx(th_m, g), ddy(th_m, g)
    grad = np.maximum(np.hypot(tx, ty), 1e-9)
    fgen = np.abs(-(tx * (ux * tx + vx * ty) + ty * (uy * tx + vy * ty)) / grad)
    return {"ti1": vws * deform, "ti2": vws * np.maximum(deform - div, 0), "invri": invri, "fgen": fgen, "vws": vws}


def to_edr(x, fit):
    """Map values onto the climatological EDR distribution, given the mean and spread of ln(x) per layer."""
    fit = np.asarray(fit)[:, :, None, None]
    z = (np.log(np.maximum(x, 1e-12)) - fit[:, 0]) / fit[:, 1]
    return np.exp(EDR_C1 + EDR_C2 * z)


def combined(diags, climate):
    """Mean of the diagnostics mapped to EDR. Averaging narrows the spread, so the mean is mapped once more."""
    mean = sum(to_edr(arr, climate["diags"][d]) for d, arr in diags.items()) / len(diags)
    mean = gaussian_filter(mean, sigma=(0, 1, 1))
    return to_edr(mean, climate["combined"]) if "combined" in climate else mean


def calibrate(samples=12, days=30):
    """Fit the mean and spread of ln(diagnostic) for each layer over past GFS runs (F006), as GTG does, so each
    diagnostic can be mapped onto the climatological EDR distribution; then the same for their average."""
    g = gfs_grid()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    kept = []
    for i in range(samples):
        cycle = now - timedelta(days=2 + i * (days - 2) / samples)
        cycle = cycle.replace(hour=(cycle.hour // 12) * 12)
        print(f"calibrate: {cycle:%Y-%m-%d %HZ}")
        try:
            f = decode_all(fetch(gfs_url(cycle, 6), gfs_wanted(6, upper_only=True)), g)
        except Exception as e:
            print(f"  skipped: {e}")
            continue
        c = column(f)
        kept.append((layer_diagnostics(c, g), c["above"][:-1] & c["above"][1:]))

    def fit(arrays):
        out = []
        for k in range(arrays[0][0].shape[0]):
            vals = np.concatenate([a[k][ok[k] & (a[k] > 0)][::4] for a, ok in arrays])
            out.append([float(np.mean(np.log(vals))), float(np.std(np.log(vals)))])
        return out

    climate = {"levels": LEVELS, "samples": len(kept), "made": now.isoformat(), "diags": {}}
    for d in TURB_DIAGS:
        climate["diags"][d] = fit([(diags[d], ok) for diags, ok in kept])
    climate["combined"] = fit([(combined(diags, climate), ok) for diags, ok in kept])
    CLIMATE.write_text(json.dumps(climate, indent=1))
    print(f"wrote {CLIMATE}")


def edr(c, g, f, climate):
    """(layer, y, x) EDR estimate: each diagnostic mapped onto the climatological EDR distribution and averaged,
    then raised where boundary-layer mixing or mountain waves are likely."""
    out = combined(layer_diagnostics(c, g), climate)

    zmid = (c["z"][1:] + c["z"][:-1]) / 2
    agl = zmid - f["zsfc"][None]
    # Boundary layer: strong mixed winds under the PBL top make mechanical turbulence.
    wind10 = np.hypot(f["u10"], f["v10"]) * KT
    gust = f.get("gust", wind10 / KT) * KT
    bl = np.clip((np.maximum(gust - 30, 0) / 15 + np.maximum(wind10 - 20, 0) / 10), 0, 1)
    in_pbl = agl <= np.maximum(f["hpbl"], 300)[None]
    out = np.where(in_pbl, np.maximum(out, 0.12 + 0.18 * bl[None]), out)

    # Mountain waves: strong flow across steep terrain with stable air near the ridge tops.
    mw, ridge = mountain_wave(c, g, f)
    near = (agl > 0) & (zmid <= ridge[None] + 3000)
    out = np.where(near, np.maximum(out, 0.12 + 0.2 * mw[None]), out)

    ok = c["above"][:-1] & c["above"][1:]
    return np.where(ok, out, 0), mw


def terrain(f, g, km=60):
    n = max(3, int(round(km / g.km())) | 1)
    zs = gaussian_filter(f["zsfc"], 0.7)
    top, low = maximum_filter(zs, n), minimum_filter(zs, n)
    return zs, top, top - low


def interp_at_height(c, field, height):
    """field (level, y, x) at height (y, x) metres MSL, linear in height."""
    z = c["z"]
    k = np.clip(np.sum(z < height[None], axis=0) - 1, 0, len(LEVELS) - 2)
    z0 = np.take_along_axis(z, k[None], 0)[0]
    z1 = np.take_along_axis(z, k[None] + 1, 0)[0]
    a0 = np.take_along_axis(field, k[None], 0)[0]
    a1 = np.take_along_axis(field, k[None] + 1, 0)[0]
    w = np.clip((height - z0) / np.maximum(z1 - z0, 1), 0, 1)
    return a0 + w * (a1 - a0)


def mountain_wave(c, g, f):
    """0–1 mountain wave score and the ridge-top height (m MSL)."""
    zs, ridge, relief = terrain(f, g)
    smooth = gaussian_filter(f["zsfc"], 2)
    hx, hy = ddx(smooth, g), ddy(smooth, g)
    slope = np.hypot(hx, hy)
    nx, ny = hx / np.maximum(slope, 1e-9), hy / np.maximum(slope, 1e-9)
    at = ridge + 500
    u, v = interp_at_height(c, c["u"], at), interp_at_height(c, c["v"], at)
    cross = np.abs(u * nx + v * ny) * KT
    theta = c["t"] * (1000.0 / c["p"]) ** 0.2857
    th_lo, th_hi = interp_at_height(c, theta, ridge), interp_at_height(c, theta, ridge + 1500)
    n2 = G / th_lo * (th_hi - th_lo) / 1500
    score = np.clip((cross - 25) / 20, 0, 1) * np.clip((relief - 500) / 700, 0, 1) * (n2 > 5e-5)
    return gaussian_filter(score, 1), ridge


# ---------------------------------------------------------------- icing and freezing levels

def freezing_levels(c, f):
    """Heights (ft MSL) of every 0 °C crossing going up (warm below, freezing above), lowest first, as a
    (n, y, x) array padded with NaN; and whether the surface itself is at or below freezing."""
    z, t, ok = c["z"], c["t"] - 273.15, c["above"]
    zs, ts = f["zsfc"], f["t2m"] - 273.15
    zz = np.concatenate([zs[None], z], 0)
    tt = np.concatenate([ts[None], np.where(ok, t, np.nan)], 0)
    crossings = []
    for k in range(zz.shape[0] - 1):
        a, b = tt[k], tt[k + 1]
        hit = (a > 0) & (b <= 0)
        w = np.clip(a / np.where(hit, a - b, 1), 0, 1)
        crossings.append(np.where(hit, (zz[k] + w * (zz[k + 1] - zz[k])) * FT, np.nan))
    cr = np.stack(crossings)
    cr = np.sort(cr, axis=0)   # NaNs sort last
    n = np.sum(np.isfinite(cr), axis=0)
    return cr[: max(int(n.max()), 1)], ts <= 0


def icing(f, c):
    """Per-level icing score (0–1), moderate-or-greater mask and supercooled large drop flag, on ICE_LEVELS."""
    n = len(ICE_LEVELS)
    t = c["t"][:n] - 273.15
    p = c["p"][:n]
    rho = p * 100 / (RD * c["t"][:n])
    clw = np.stack([f[f"clw{q}"] for q in ICE_LEVELS])
    rw = np.stack([f[f"rw{q}"] for q in ICE_LEVELS])
    ice = np.stack([f[f"ci{q}"] + f[f"sn{q}"] + f[f"gr{q}"] for q in ICE_LEVELS])
    rh = np.stack([f[f"rh{q}"] for q in ICE_LEVELS])
    w = np.stack([f[f"w{q}"] for q in ICE_LEVELS])
    lwc = (clw + rw) * rho * 1000   # g/m³
    cloudy = (rh >= 85) | (clw + ice > 1e-6)

    # Cloud-top temperature of the cloud layer each level sits in: scan down from the top of the column.
    ctt = np.full(t.shape, np.nan, dtype=np.float32)
    top_t = np.full(t.shape[1:], np.nan, dtype=np.float32)
    for k in range(n - 1, -1, -1):
        top_t = np.where(cloudy[k], np.where(np.isnan(top_t), t[k], top_t), np.nan)
        ctt[k] = top_t

    t_int = np.interp(t, [-25, -15, -5, 0], [0, 1, 1, 0], left=0, right=0)
    moist = np.maximum(np.interp(lwc, [0.01, 0.2], [0, 1]), 0.6 * np.interp(rh, [80, 97], [0, 1]))
    scen = np.where(ctt >= -12, 1.0, np.interp(np.nan_to_num(ctt, nan=-40), [-25, -12], [0.55, 1.0]))
    scen = np.where(lwc >= 0.05, np.maximum(scen, 0.85), scen)
    glaciated = ice > 3 * np.maximum(clw + rw, 1e-9)
    scen = np.where(glaciated & (lwc < 0.05), scen * 0.6, scen)
    lift = 1 + 0.3 * np.clip(-w / 0.5, 0, 1)   # VVEL is Pa/s; negative is rising
    score = np.clip(t_int * moist * scen * lift, 0, 1) * cloudy * c["above"][:n]
    score = gaussian_filter(score, sigma=(0, 0.8, 0.8))
    moderate = (score >= 0.5) & ((lwc >= 0.05) | ((rh >= 95) & (w < -0.3)))
    moderate = binary_dilation(moderate, structure=np.ones((1, 3, 3)))

    # Freezing rain and drizzle: a melting layer aloft over a sub-freezing layer, with precipitation.
    warm_aloft = np.any((t > 0) & c["above"][:n], axis=0) & (f["t2m"] - 273.15 <= 0.5)
    sld = (warm_aloft & (f["prate"] > 2e-5)) | (f.get("cfrzr", 0) > 0.5)
    return score, moderate, sld, lwc


# ---------------------------------------------------------------- grids to shapes

def alt_label(ft):
    if ft <= 0:
        return "SFC"
    return f"FL{int(round(ft / 1000)) * 10:03d}" if ft >= 18000 else f"{int(round(ft / 100)):03d}"


def shapes(cover, g, level=0.5, min_area=MIN_AREA_SQMI, smooth_km=25):
    """Polygons (in grid index space) where cover >= level, smoothed and simplified, at least min_area sq mi."""
    z = np.nan_to_num(cover).astype(np.float64)
    gen = contourpy.contour_generator(z=z, fill_type="OuterOffset")
    pts_list, offs_list = gen.filled(level, np.inf)
    r = smooth_km / g.km()
    out = []
    for pts, offs in zip(pts_list, offs_list):
        rings = [pts[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]
        if len(rings[0]) < 4:
            continue
        poly = shapely.make_valid(Polygon(rings[0], [h for h in rings[1:] if len(h) >= 4]))
        poly = poly.buffer(r).buffer(-r).simplify(r * 0.6)
        for part in getattr(poly, "geoms", [poly]):
            if part.geom_type != "Polygon" or part.is_empty:
                continue
            cy, cx = part.centroid.y, part.centroid.x
            cell = g.area_sqmi[int(np.clip(round(cy), 0, g.shape[0] - 1)), int(np.clip(round(cx), 0, g.shape[1] - 1))]
            area = part.area * cell
            if area >= min_area:
                holes = [h for h in part.interiors if Polygon(h).area * cell >= min_area / 2]
                out.append((Polygon(part.exterior, holes), area))
    return out


def inside(poly, g):
    """Boolean grid of the cells whose centres fall inside a polygon in index space."""
    minx, miny, maxx, maxy = poly.bounds
    m = np.zeros(g.shape, dtype=bool)
    y0, y1 = max(int(miny), 0), min(int(maxy) + 2, g.shape[0])
    x0, x1 = max(int(minx), 0), min(int(maxx) + 2, g.shape[1])
    yy, xx = np.mgrid[y0:y1, x0:x1]
    m[y0:y1, x0:x1] = shapely.contains_xy(poly, xx, yy)
    return m


def to_lonlat(geom, g):
    """A shape in grid index space -> the same shape in lon/lat."""
    def conv(coords):
        a = np.asarray(coords)
        lat = map_coordinates(g.lat, [a[:, 1], a[:, 0]], order=1, mode="nearest")
        lon = map_coordinates(g.lon, [a[:, 1], a[:, 0]], order=1, mode="nearest")
        return np.round(np.column_stack([lon, lat]), 3)
    return shapely.transform(geom, lambda xy: conv(xy))


def feature(geom, g, **props):
    return {"type": "Feature", "geometry": mapping(to_lonlat(geom, g)), "properties": props}


def coverage(mask, g, km=60):
    n = max(3, int(round(km / g.km())) | 1)
    return uniform_filter(mask.astype(np.float32), n)


def pct(a, q, default=0.0):
    a = a[np.isfinite(a)]
    return float(np.percentile(a, q)) if a.size else default


def round_base(ft, terrain_ft):
    if ft <= terrain_ft + 1000:
        return 0
    return int(math.floor(ft / 1000) * 1000)


def round_top(ft):
    return int(math.ceil(ft / 1000) * 1000)


# ---------------------------------------------------------------- hazards shared by the models

def ifr_features(g, ceil_agl_ft, vis_sm, prate, spread_c, series_note=""):
    low_cig = ceil_agl_ft < 1000
    low_vis = vis_sm < 3
    ifr = low_cig | low_vis
    feats = []
    for poly, area in shapes(coverage(ifr, g), g):
        m = inside(poly, g) & ifr
        if not m.any():
            continue
        cause = []
        if low_cig[m].mean() >= 0.3:
            cause.append("CIG BLW 010")
        if low_vis[m].mean() >= 0.3:
            obsc = []
            if (prate[m & low_vis] > 1e-4).mean() >= 0.3:
                obsc.append("PCPN")
            if (spread_c[m & low_vis] <= 2).mean() >= 0.3:
                obsc.append("BR/FG")
            cause.append("VIS BLW 3SM" + (" " + "/".join(obsc) if obsc else ""))
        feats.append(feature(poly, g, hazard="IFR", series="Sierra", base_ft=0, top_ft=1000, base="SFC", top="010",
                             area_sqmi=round(area), cause=" · ".join(cause) or "CIG BLW 010/VIS BLW 3SM",
                             coverage=round(float(ifr[inside(poly, g)].mean()) * 100)))
    return feats


def mtn_features(g, f, cloud_base_msl_m, vis_sm):
    zs, ridge, relief = terrain(f, g, km=40)
    mountains = (ridge >= 900) & (relief >= 450)
    obscured = mountains & ((cloud_base_msl_m <= ridge) | (vis_sm < 1))
    feats = []
    for poly, area in shapes(coverage(obscured, g) * mountains, g, level=0.5):
        m = inside(poly, g) & obscured
        if not m.any():
            continue
        top = round_top(pct(ridge[m], 90) * FT)
        feats.append(feature(poly, g, hazard="MTN OBSCN", series="Sierra", base_ft=0, top_ft=top, base="SFC",
                             top=alt_label(top), area_sqmi=round(area), cause="MTNS OBSC BY CLDS"
                             + ("/PCPN" if (vis_sm[m] < 3).mean() > 0.3 else "")))
    return feats


# ---------------------------------------------------------------- GFS hazards

def gfs_hazards(f, g, climate):
    c = column(f)
    feats = []
    zs_ft = f["zsfc"] * FT

    # Sierra
    ceil = f["ceil"]
    ceil_agl = np.where((ceil < 15000) & np.isfinite(ceil), (ceil - f["zsfc"]) * FT, 99999)
    vis_sm = f["vis"] / 1609.34
    feats += ifr_features(g, ceil_agl, vis_sm, f["prate"], f["t2m"] - f["d2m"])
    feats += mtn_features(g, f, np.where(ceil < 15000, ceil, 99999), vis_sm)

    # Tango
    e, mw = edr(c, g, f, climate)
    alt = level_alt_ft(c)
    lo_ft, hi_ft = alt[:-1], alt[1:]
    upper = (c["p"][1:] <= 500)[:, 0, 0]   # layers whose top is at or above 500 mb
    for name, band in (("TURB-HI", upper), ("TURB-LO", ~upper)):
        mod = (e >= EDR_MOD) & band[:, None, None]
        any_mod = mod.any(0)
        base = np.where(mod, lo_ft, np.inf).min(0)
        top = np.where(mod, hi_ft, -np.inf).max(0)
        peak = np.where(band[:, None, None], e, 0).max(0)
        for poly, area in shapes(coverage(any_mod, g, km=75), g, level=0.4):
            m = inside(poly, g) & any_mod
            if not m.any():
                continue
            b = round_base(pct(base[m], 10), pct(zs_ft[m], 10))
            t = round_top(pct(top[m], 90))
            if name == "TURB-HI":
                b, t = max(b, 18000), min(max(t, b + 2000), 45000)
            else:
                t = min(t, 18000)
            reasons = []
            if (mw[m] > 0.3).mean() > 0.2:
                reasons.append("mountain wave")
            sev = (peak[m] >= EDR_SEV).mean() > 0.1
            feats.append(feature(poly, g, hazard=name, series="Tango", base_ft=b, top_ft=t, base=alt_label(b), top=alt_label(t),
                                 area_sqmi=round(area), peak_edr=round(pct(peak[m], 95), 2),
                                 severity="MOD OCNL SEV" if sev else "MOD", cause=", ".join(reasons)))

    wind10 = np.hypot(f["u10"], f["v10"]) * KT
    for poly, area in shapes(coverage(wind10 >= 30, g), g):
        m = inside(poly, g)
        feats.append(feature(poly, g, hazard="SFC WND", series="Tango", base_ft=0, top_ft=0, base="SFC", top="SFC",
                             area_sqmi=round(area), peak_kt=int(pct(wind10[m], 95))))

    at = f["zsfc"] + 2000 / FT
    u2k, v2k = interp_at_height(c, c["u"], at), interp_at_height(c, c["v"], at)
    shear = np.hypot(u2k - f["u10"], v2k - f["v10"]) * KT
    llws = shear >= 30
    for poly, area in shapes(coverage(llws, g), g):
        m = inside(poly, g)
        feats.append(feature(poly, g, hazard="LLWS", series="Tango", base_ft=0, top_ft=2000, base="SFC", top="2,000 ft AGL",
                             area_sqmi=round(area), peak_kt=int(pct(shear[m], 95))))

    # Zulu
    score, mod, sld, lwc = icing(f, c)
    n = len(ICE_LEVELS)
    ice_alt = level_alt_ft(c)[:n]
    fz, sfc_frz = freezing_levels(c, f)
    lowest_fz = np.where(sfc_frz, 0, fz[0])
    any_ice = mod.any(0)
    ibase = np.where(mod, ice_alt, np.inf).min(0)
    itop = np.where(mod, ice_alt, -np.inf).max(0)
    for poly, area in shapes(coverage(any_ice, g, km=75), g, level=0.4):
        m = inside(poly, g) & any_ice
        if not m.any():
            continue
        b = round_base(pct(ibase[m], 10), pct(zs_ft[m], 10))
        t = round_top(pct(itop[m], 90) + 1000)
        fzl = pct(np.where(np.isfinite(lowest_fz[m]), lowest_fz[m], np.nan), 50, default=0)
        base_txt = "FRZLVL" if b > 0 and abs(b - fzl) <= 1500 else alt_label(b)
        fz_lo, fz_hi = pct(lowest_fz[m], 10, 0), pct(lowest_fz[m], 90, 0)
        feats.append(feature(poly, g, hazard="ICE", series="Zulu", base_ft=b, top_ft=t, base=base_txt, top=alt_label(t),
                             area_sqmi=round(area), peak=round(pct(score.max(0)[m], 95), 2),
                             lwc=round(pct(np.where(mod, lwc, np.nan).max(0)[m], 90, 0), 2),
                             fzlvl=f"{alt_label(round(fz_lo / 1000) * 1000)}–{alt_label(round(fz_hi / 1000) * 1000)}",
                             sld=bool(sld[m].mean() > 0.2)))

    multi = np.sum(np.isfinite(fz), 0) >= 2
    for poly, area in shapes(coverage(multi, g), g):
        m = inside(poly, g) & multi
        if not m.any():
            continue
        b = round_base(pct(fz[0][m], 10), -9999)
        t = round_top(pct(np.nanmax(fz, 0)[m], 90))
        feats.append(feature(poly, g, hazard="M_FZLVL", series="Zulu", base_ft=b, top_ft=t, base=alt_label(b), top=alt_label(t),
                             area_sqmi=round(area)))

    # Freezing level lines every 4,000 ft, and the surface freezing line.
    fz_field = gaussian_filter(np.where(sfc_frz, -2000, np.nan_to_num(fz[0], nan=30000)), 1.2)
    gen = contourpy.contour_generator(z=fz_field.astype(np.float64), line_type="Separate")
    for level in (0, 4000, 8000, 12000, 16000):
        for line in gen.lines(level):
            if len(line) < 12:
                continue
            ls = LineString(line).simplify(0.6)
            feats.append(feature(ls, g, hazard="FZLVL", series="Zulu", level_ft=level,
                                 base_ft=level, top_ft=level, base=alt_label(level), top=alt_label(level)))
    return feats


# ---------------------------------------------------------------- HRRR

HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
HRRR_STEP = 2   # every other point: 6 km
HRRR_CONE = np.sin(np.radians(38.5))


def hrrr_url(cycle, fhr):
    return f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf{fhr:02d}.grib2"


def hrrr_grid(blob):
    h = eccodes.codes_new_from_message(blob)
    try:
        nx, ny = eccodes.codes_get(h, "Nx"), eccodes.codes_get(h, "Ny")
        lat = eccodes.codes_get_array(h, "latitudes").reshape(ny, nx)
        lon = eccodes.codes_get_array(h, "longitudes").reshape(ny, nx)
    finally:
        eccodes.codes_release(h)
    lon = (lon + 180) % 360 - 180
    # HRRR rows run south to north; flip so that "down a row" is south, like the GFS grid.
    cut = (slice(None, None, -HRRR_STEP), slice(None, None, HRRR_STEP))
    return Grid(lat[cut], lon[cut], cut, 3.0 * HRRR_STEP)


def hrrr_wanted(fhr):
    a = f"{fhr} hour fcst$"
    m = f"{max(fhr - 1, 0)}-{fhr} hour max fcst$"
    w = {
        "refc": f"REFC:entire atmosphere:{a}", "retop": f"RETOP:cloud top:{a}", "ltng": f"LTNG:entire atmosphere:{a}",
        "vis": f"VIS:surface:{a}", "ceil": f"HGT:cloud ceiling:{a}", "cbase": f"HGT:cloud base:{a}",
        "zsfc": f"HGT:surface:{a}", "t2m": f"TMP:2 m above ground:{a}", "d2m": f"DPT:2 m above ground:{a}",
        "prate": f"PRATE:surface:{a}",
        "u700": f"UGRD:700 mb:{a}", "v700": f"VGRD:700 mb:{a}", "u500": f"UGRD:500 mb:{a}", "v500": f"VGRD:500 mb:{a}",
    }
    if fhr > 0:
        w.update({"uh": f"MXUPHL:5000-2000 m above ground:{m}", "hail": f"HAIL:surface:{m}",
                  "wmax": f"WIND:10 m above ground:{m}"})
    return w


def motion(prev, cur, mask, g, f):
    """Storm motion (direction it comes from, kt) inside mask, by matching reflectivity between hours; falls back to
    the 700–500 mb mean wind. Returns (from_deg, kt, dy_cells, dx_cells) per hour."""
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    best = None
    if prev is not None:
        win = 18
        a = np.nan_to_num(cur[y0:y1, x0:x1]) >= 30
        if a.sum() > 20:
            for dy in range(-win, win + 1, 1):
                for dx in range(-win, win + 1, 1):
                    yy0, xx0 = y0 - dy, x0 - dx
                    if yy0 < 0 or xx0 < 0 or yy0 + (y1 - y0) > g.shape[0] or xx0 + (x1 - x0) > g.shape[1]:
                        continue
                    b = np.nan_to_num(prev[yy0:yy0 + (y1 - y0), xx0:xx0 + (x1 - x0)]) >= 30
                    score = (a & b).sum() / max((a | b).sum(), 1)
                    if best is None or score > best[0]:
                        best = (score, dy, dx)
    cy, cx = int(np.mean(ys)), int(np.mean(xs))
    if best and best[0] >= 0.25:
        _, dy, dx = best
    else:
        lon = g.lon[cy, cx]
        a = np.radians(HRRR_CONE * (lon + 97.5))
        ug = (f["u700"][mask].mean() + f["u500"][mask].mean()) / 2
        vg = (f["v700"][mask].mean() + f["v500"][mask].mean()) / 2
        u = np.cos(a) * ug + np.sin(a) * vg   # earth-relative, m/s
        v = -np.sin(a) * ug + np.cos(a) * vg
        dx = u * 3600 / (g.cell_km * 1000)
        dy = -v * 3600 / (g.cell_km * 1000)   # rows run south
    lat0, lon0 = g.lat[cy, cx], g.lon[cy, cx]
    yy, xx = np.clip(cy + dy, 0, g.shape[0] - 1), np.clip(cx + dx, 0, g.shape[1] - 1)
    lat1 = map_coordinates(g.lat, [[yy], [xx]], order=1)[0]
    lon1 = map_coordinates(g.lon, [[yy], [xx]], order=1)[0]
    east = (lon1 - lon0) * 111.195 * math.cos(math.radians(lat0))
    north = (lat1 - lat0) * 111.195
    kt = math.hypot(east, north) / 1.852
    toward = math.degrees(math.atan2(east, north)) % 360
    return (round((toward + 180) % 360 / 10) * 10 % 360 or 360), int(round(kt / 5) * 5), dy, dx


def convective_features(f, g, prev_refc):
    refc = np.nan_to_num(f["refc"], nan=-10)
    ltng = np.nan_to_num(f.get("ltng", np.zeros_like(refc)))
    storm = (refc >= 40) | ((refc >= 35) & (ltng > 0.5))
    footprint = binary_dilation(storm, iterations=1)
    feats = []
    taken = np.zeros(g.shape, dtype=bool)
    cover = coverage(footprint, g, km=50)
    for poly, area in shapes(cover, g, level=0.4, smooth_km=15):
        m = inside(poly, g)
        cells = m & storm
        if cells.sum() < 6:
            continue
        taken |= m
        tops_m = np.nan_to_num(f["retop"][cells], nan=0)
        tops_ft = pct(tops_m * FT, 90)
        tops = "ABV FL450" if tops_ft > 45000 else f"FL{int(round(tops_ft / 1000)) * 10:03d}"
        frm, kt, dy, dx = motion(prev_refc, f["refc"], cells, g, f)
        tags = []
        if "uh" in f and pct(np.nan_to_num(f["uh"][m]), 99) >= 75:
            tags.append("SUPERCELLS POSS")
        if "hail" in f:
            hail_in = pct(np.nan_to_num(f["hail"][m]) * 39.37, 99)
            if hail_in >= 0.75:
                tags.append(f"HAIL TO {math.floor(hail_in * 4) / 4:g} IN")
        if "wmax" in f:
            gust = pct(np.nan_to_num(f["wmax"][m]) * KT, 99)
            if gust >= 50:
                tags.append(f"WIND GUSTS TO {int(round(gust / 5) * 5)}KT")

        # Long, narrow groups are written as a line with a width, the way real convective SIGMETs are.
        ys, xs = np.nonzero(cells)
        pts = np.column_stack([xs, ys]).astype(float)
        mean = pts.mean(0)
        evals, evecs = np.linalg.eigh(np.cov((pts - mean).T) + np.eye(2) * 1e-6)
        major = evecs[:, 1]
        proj = (pts - mean) @ major
        length_nm = (proj.max() - proj.min()) * g.cell_km / 1.852
        width_nm = 4 * math.sqrt(max(evals[0], 0)) * g.cell_km / 1.852
        props = dict(series="Convective", base_ft=0, top_ft=int(min(tops_ft, 50000)), base="SFC", top=tops,
                     tops=tops, area_sqmi=round(area), mov_from=frm, mov_kt=kt, tags=tags,
                     coverage=round(float(storm[m].mean() * 100)), motion=[float(dx), float(dy)])
        if length_nm >= 60 and length_nm >= 3.5 * width_nm:
            a, b = mean + major * proj.min(), mean + major * proj.max()
            line = LineString([a, b])
            w = max(20, int(math.ceil(width_nm / 10) * 10))
            props.update(hazard="TS LINE", width_nm=w, length_nm=int(round(length_nm / 10) * 10))
            feats.append(feature(line.buffer(w * 1.852 / g.cell_km / 2, cap_style="round"), g, **props))
        else:
            props.update(hazard="TS AREA")
            feats.append(feature(poly, g, **props))

    # Strong single cells outside those areas.
    strong = storm & ~taken
    if "uh" in f:
        strong &= (refc >= 50) & ((np.nan_to_num(f["uh"]) >= 75) | (np.nan_to_num(f.get("hail", 0)) * 39.37 >= 1))
    else:
        strong &= refc >= 55
    lab, n = label(binary_dilation(strong, iterations=2))
    for i in range(1, n + 1):
        cell = lab == i
        if cell.sum() < 3:
            continue
        ys, xs = np.nonzero(cell)
        cy, cx = ys.mean(), xs.mean()
        tops_ft = pct(np.nan_to_num(f["retop"][cell]) * FT, 90)
        tops = "ABV FL450" if tops_ft > 45000 else f"FL{int(round(tops_ft / 1000)) * 10:03d}"
        frm, kt, dy, dx = motion(prev_refc, f["refc"], cell, g, f)
        geom = shapely.Point(cx, cy).buffer(10 * 1.852 / g.cell_km)
        feats.append(feature(geom, g, hazard="TS ISOL", series="Convective", base_ft=0, top_ft=int(min(tops_ft, 50000)),
                             base="SFC", top=tops, tops=tops, diameter_nm=20, mov_from=frm, mov_kt=kt,
                             tags=["SEV TS POSS"], motion=[float(dx), float(dy)]))
    return feats


def hrrr_hazards(f, g, prev_refc):
    feats = []
    zs = f["zsfc"]
    ceil = f["ceil"]
    # HRRR ceilings are heights above sea level; very large values mean none.
    ceil_agl = np.where(np.isfinite(ceil) & (ceil > -1000) & (ceil < 15000), (ceil - zs) * FT, 99999)
    vis_sm = np.nan_to_num(f["vis"], nan=99999) / 1609.34
    feats += ifr_features(g, ceil_agl, vis_sm, f["prate"], f["t2m"] - f["d2m"])
    # The cloud base, unlike the ceiling, is given above ground.
    cbase = f["cbase"]
    cbase = np.where(np.isfinite(cbase) & (cbase >= 0) & (cbase < 15000), cbase + zs, 99999)
    feats += mtn_features(g, f, np.minimum(cbase, np.where(ceil_agl < 99999, ceil, 99999)), vis_sm)
    feats += convective_features(f, g, prev_refc)
    return feats


# ---------------------------------------------------------------- run

def clip(feats):
    """Clip every shape to DOMAIN, dropping those that fall mostly outside it."""
    out = []
    for ft in feats:
        geom = shapely.make_valid(shape(ft["geometry"]))
        cut = geom.intersection(DOMAIN)
        if cut.geom_type == "GeometryCollection":   # keep only the parts of the shape's own kind
            parts = [p for p in cut.geoms if p.geom_type.endswith(geom.geom_type.removeprefix("Multi"))]
            cut = shapely.union_all(parts) if parts else shapely.Polygon()
        if cut.is_empty:
            continue
        if geom.area > 0 and cut.area < 0.25 * geom.area:
            continue
        if geom.area > 0 and "area_sqmi" in ft["properties"]:
            ft["properties"]["area_sqmi"] = round(ft["properties"]["area_sqmi"] * cut.area / geom.area)
            if ft["properties"]["area_sqmi"] < MIN_AREA_SQMI and ft["properties"]["hazard"] != "TS ISOL":
                continue
        cut = shapely.set_precision(cut, 0.001)
        ft["geometry"] = mapping(cut)
        out.append(ft)
    return out


def write_hour(model_id, fhr, valid, feats):
    feats = clip(feats)
    d = OUT / model_id
    d.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "valid": valid.isoformat(), "fhr": fhr, "features": feats}
    (d / f"f{fhr:03d}.geojson").write_text(json.dumps(fc, separators=(",", ":")))


def summary(feats):
    out = {}
    for ft in feats:
        h = ft["properties"]["hazard"]
        out[h] = out.get(h, 0) + 1
    return out


def run_gfs(hours):
    climate = json.loads(CLIMATE.read_text())
    g = gfs_grid()
    cycle = latest_cycle(gfs_url, hours[-1])
    print(f"GFS: cycle {cycle:%Y-%m-%d %HZ}")
    done = []
    for fhr in hours:
        t0 = time.time()
        try:
            f = decode_all(fetch(gfs_url(cycle, fhr), gfs_wanted(fhr)), g)
            feats = gfs_hazards(f, g, climate)
        except Exception as e:   # one bad hour shouldn't sink the run
            print(f"  F{fhr:03d} skipped: {e!r}")
            continue
        write_hour("gfs", fhr, cycle + timedelta(hours=fhr), feats)
        done.append(fhr)
        print(f"  F{fhr:03d} {time.time() - t0:.0f}s {summary(feats)}")
    return cycle, done


def run_hrrr(hours):
    # The newest run that has reached its last hour: an 00, 06, 12 or 18Z run when that is F48.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cycle = None
    for back in range(0, 8):
        c = now - timedelta(hours=back)
        if session.head(hrrr_url(c, hours[-1]) + ".idx", timeout=30).status_code == 200:
            cycle = c
            break
    if cycle is None:
        raise SystemExit("hrrr: no complete run in the last 8 hours")
    print(f"HRRR: cycle {cycle:%Y-%m-%d %HZ}")
    blobs = fetch(hrrr_url(cycle, 0), {"refc": "REFC:entire atmosphere:anl$"})
    g = hrrr_grid(blobs["refc"])
    prev = decode(blobs["refc"], g)
    done = []
    for fhr in hours:
        t0 = time.time()
        try:
            f = decode_all(fetch(hrrr_url(cycle, fhr), hrrr_wanted(fhr)), g)
            feats = hrrr_hazards(f, g, prev)
            prev = f["refc"]
        except Exception as e:
            print(f"  F{fhr:02d} skipped: {e!r}")
            prev = None
            continue
        write_hour("hrrr", fhr, cycle + timedelta(hours=fhr), feats)
        done.append(fhr)
        print(f"  F{fhr:02d} {time.time() - t0:.0f}s {summary(feats)}")
    return cycle, done


MODELS = {
    "gfs": {"name": "GFS", "long": "GFS 0.25°", "hours": list(range(3, 49, 3)) + list(range(54, 169, 6)), "run": run_gfs,
            "hazards": ["IFR", "MTN OBSCN", "TURB-HI", "TURB-LO", "SFC WND", "LLWS", "ICE", "M_FZLVL", "FZLVL"]},
    "hrrr": {"name": "HRRR", "long": "HRRR 3 km", "hours": list(range(1, 49)), "run": run_hrrr,
             "hazards": ["IFR", "MTN OBSCN", "TS AREA", "TS LINE", "TS ISOL"]},
}


def main(model_id):
    m = MODELS[model_id]
    hours = m["hours"][: int(os.environ["FW_HOURS"])] if os.environ.get("FW_HOURS") else m["hours"]
    cycle, done = m["run"](hours)
    if not done:
        raise SystemExit(f"{model_id}: no forecast hours drawn")
    info = {"id": model_id, "name": m["name"], "long": m["long"], "cycle": cycle.strftime("%Y-%m-%dT%HZ"),
            "init": cycle.isoformat(), "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hours": done, "hazards": m["hazards"], "min_area_sqmi": MIN_AREA_SQMI, "note": NOTE}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{model_id}.json").write_text(json.dumps(info, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in [*MODELS, "calibrate"]:
        raise SystemExit(f"usage: make_airmetcast.py {{{','.join(MODELS)},calibrate}}")
    calibrate() if sys.argv[1] == "calibrate" else main(sys.argv[1])
