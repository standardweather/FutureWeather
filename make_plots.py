"""Download the latest model runs and draw transparent map overlays for the FutureWeather page.

Models (all from NOAA's open-data buckets on AWS; only the needed fields are downloaded, using the
byte offsets in each file's .idx index):
  gfs   Global Forecast System 0.25°, F006–F168 every 6 hours
  nbm   National Blend of Models (the basis of NWS forecasts), F006–F168 every 6 hours
  hrrr  High-Resolution Rapid Refresh 3 km, F001–F048 hourly
  ecmwf ECMWF IFS 0.25° open data (CC BY 4.0), F006–F168 every 6 hours, from its 00 and 12Z runs

    python make_plots.py gfs        # or nbm, hrrr, ecmwf

Output, per model:
  site/img/<model>/<product>_f<hhh>.png   one transparent overlay per product per forecast hour, or for a product
                                          made of layers (cloud cover, pressure lines, ...), one per layer:
  site/img/<model>/<product>-<layer>_f<hhh>.png   so the page can turn each part on and off
  site/img/<model>/surface_f<hhh>.json    forecast station plot values for the Surface map (see site/stations.json)
  site/models/<model>.json                run info, products and legends

Overlays are drawn in Web Mercator over BOUNDS, so the page lays them straight onto its Leaflet map.
merge_runs.py combines the per-model files into site/data.json.

The Upper air map's normalized 500 mb height anomaly uses z500_climate.npz, built once by make_climatology.py.

FW_HOURS=2 limits each model to its first two forecast hours (for local testing).
"""

import io
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import metpy.calc as mpcalc
import numpy as np
import requests
import xarray as xr
from PIL import Image
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

OUT = Path(__file__).parent / "site"

# Overlay extent (degrees) and size. Mercator rows make the image line up with the web map.
BOUNDS = {"south": 20.0, "north": 54.0, "west": -130.0, "east": -60.0}


def merc(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


X0, X1 = np.radians(BOUNDS["west"]), np.radians(BOUNDS["east"])
Y0, Y1 = merc(BOUNDS["south"]), merc(BOUNDS["north"])
WIDTH = 1800
HEIGHT = round(WIDTH * (Y1 - Y0) / (X1 - X0))

ESTIMATE_NOTE = ("Diagnostic estimate calculated from GFS output, not an official aviation "
                 "forecast. See aviationweather.gov for operational guidance.")

session = requests.Session()
session.headers["User-Agent"] = "FutureWeather (standardweather.github.io)"


# ---------------------------------------------------------------- color scales

def binned(levels, colors):
    cmap = mcolors.ListedColormap(colors)
    return cmap, mcolors.BoundaryNorm(levels, cmap.N)


def ramp(levels, stops):
    """A smooth colormap through (value, color) stops, cut into the bins between levels."""
    lo, hi = stops[0][0], stops[-1][0]
    smooth = mcolors.LinearSegmentedColormap.from_list("r", [((v - lo) / (hi - lo), c) for v, c in stops])
    centers = [(a + b) / 2 for a, b in zip(levels[:-1], levels[1:])]
    return binned(levels, [mcolors.to_hex(smooth((c - lo) / (hi - lo))) for c in centers])


def legend(label, levels, cmap, lines=None, extra=None):
    """What the page needs to draw a legend: bin edges and colors, plus notes on contour lines."""
    out = {"label": label, "levels": [float(f"{v:g}") for v in levels],
           "colors": [mcolors.to_hex(cmap(i)) for i in range(cmap.N)]}
    if lines:
        out["lines"] = lines
    if extra:
        out["extra"] = extra
    return out


PRECIP_LEVELS = [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6]
PRECIP = binned(PRECIP_LEVELS, ["#c8f0c0", "#8fd88a", "#41ab5d", "#1f7f3b", "#0a5a2a",
                                "#f6e33c", "#f2a33a", "#e8532b", "#c41d1d", "#a0157a", "#d38ae8"])

# Weak vorticity (|ζ| < 4) is left clear; stronger values get brighter in each direction.
VORT_LEVELS = [-24, -20, -16, -12, -8, -4, 4, 8, 12, 16, 20, 24]
VORT = binned(VORT_LEVELS, ["#9be7ff", "#4ad1ff", "#2f9fd8", "#2a6fb0", "#24497f", (0, 0, 0, 0),
                            "#7a2f3a", "#b8423f", "#e0603a", "#f5923e", "#ffd166"])

JET_LEVELS = list(range(50, 201, 10))
JET = ramp(JET_LEVELS, [(50, "#1b4f7a"), (90, "#4ad1ff"), (130, "#8f6bff"), (165, "#e04fd0"), (200, "#ffd1f5")])

CLOUD_LEVELS = [10, 30, 50, 70, 90, 100]
CLOUD = binned(CLOUD_LEVELS, ["#2a3358", "#46507a", "#6f789e", "#a3aac6", "#dfe3f1"])
CEILING_CATS = [  # (upper bound ft AGL, label, color): standard flight-category colors
    (500, "LIFR < 500 ft", "#e04fd0"),
    (1000, "IFR 500–999 ft", "#ff453a"),
    (3000, "MVFR 1,000–3,000 ft", "#4ad1ff"),
]
CEILING_LAYERS = ["lifr", "ifr", "mvfr"]   # layer ids, in CEILING_CATS order

# Shared hazard scale for turbulence and icing: muted teal for low values, then cyan, amber, orange, red.
HAZARD_COLORS = ["#2b6f86", "#4ad1ff", "#ffd166", "#ff9f43", "#ff453a"]
# The published Ellrod thresholds (4/8/12) were tuned on coarser grids. At 0.25° the gradients are
# sharper and values run about 3x higher, so the scale is shifted up: roughly 10% of CONUS gets any
# shading and ~1% reaches orange or red.
TURB_LEVELS = [12, 20, 30, 40, 50, 60]
TURB = binned(TURB_LEVELS, HAZARD_COLORS)
# Humidity-only areas top out at 0.6; amber and above need model cloud liquid water.
ICE_LEVELS = [0.2, 0.4, 0.6, 0.7, 0.85, 1.0]
ICE = binned(ICE_LEVELS, HAZARD_COLORS)

TEMP_LEVELS = list(range(-20, 111, 5))
TEMP = ramp(TEMP_LEVELS, [(-20, "#f0d8ff"), (0, "#9b6bd6"), (15, "#3f4fc4"), (32, "#2f8fe0"), (40, "#4ad1ff"),
                          (50, "#3fcf9a"), (60, "#8fd84a"), (70, "#f6e33c"), (80, "#f2a33a"), (90, "#e8532b"),
                          (100, "#c41d1d"), (110, "#ff7ab8")])

GUST_LEVELS = [20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 100]
GUST = binned(GUST_LEVELS, ["#2b6f86", "#3a9fbf", "#4ad1ff", "#8f6bff", "#c05cff",
                            "#ffd166", "#ff9f43", "#ff453a", "#d40000", "#ff7ab8"])

SNOW_LEVELS = [0.1, 0.5, 1, 2, 3, 4, 6, 8, 12, 18, 24]
SNOW = binned(SNOW_LEVELS, ["#c9e6ff", "#9ccbff", "#6fb0ff", "#4f8dff", "#3f63e0",
                            "#5a3fc4", "#8a3fc4", "#b84fd6", "#e07ae8", "#ffc8f5"])

CAPE_LEVELS = [250, 500, 1000, 1500, 2000, 2500, 3000, 4000, 5000]
CAPE = binned(CAPE_LEVELS, ["#2b4f6f", "#2f7fa8", "#3fcf9a", "#9be15d", "#f6e33c", "#f2a33a", "#e8532b", "#c41d1d"])

UH_LEVELS = [25, 50, 75, 100, 150, 200, 300]
UH = binned(UH_LEVELS, ["#4ad1ff", "#8f6bff", "#ffd166", "#ff9f43", "#ff453a", "#ff7ab8"])

# Surface map: cloud cover in grays (clear sky leaves the dark base map showing), with precipitation rate
# on top in one ramp per type, light to heavy.
SKY_LEVELS = [10, 20, 40, 60, 80, 90, 100]
SKY = binned(SKY_LEVELS, ["#34373f", "#454851", "#5b5e67", "#74777f", "#92959c", "#b4b6bb"])
RATE_LEVELS = [0.004, 0.01, 0.03, 0.1, 0.25, 0.5, 1, 2]   # in/h; 0.004 is about 0.1 mm/h
PTYPES = [  # (code, name, colors). Codes are what the station files and draw_surface use.
    (1, "Rain", ["#a8eda0", "#5cd25c", "#1f9a3a", "#f6e33c", "#f2902a", "#d42020", "#e040e0"]),
    (2, "Snow", ["#c9f0ff", "#8fd8ff", "#4ab3ff", "#2f7fe8", "#2450c8", "#1f2fa0", "#4a1f8a"]),
    (3, "Ice pellets", ["#e6d0ff", "#c9a8f5", "#ad84e8", "#9060d8", "#7440c0", "#5a28a0", "#401480"]),
    (4, "Freezing rain", ["#ffd0e4", "#ffa0c8", "#ff6fae", "#f03c90", "#c81e70", "#9a0f55", "#6e063c"]),
]

# Cloud top height (thousands of ft): grays for low and mid-level tops, brightening to white near 30,000 ft,
# then the cold colors of enhanced satellite imagery for the tallest tops (thunderstorms, deep cirrus).
TOPS_LEVELS = [0, 3, 6, 10, 15, 20, 25, 30, 35, 40, 45, 50]
TOPS = binned(TOPS_LEVELS, ["#3a3f4b", "#4f5563", "#686f7e", "#858c9b", "#a5abb7", "#c8ccd4", "#eef0f4",
                            "#4ad1ff", "#2f7fe8", "#8f6bff", "#e04fd0"])
ECHO_TOPS = [25, 35, 45]   # thousands of ft; echo top contours
FT = 3.28084

# NWS radar colors.
REFL_LEVELS = list(range(5, 80, 5))
REFL = binned(REFL_LEVELS, ["#04e9e7", "#019ff4", "#0300f4", "#02fd02", "#01c501", "#008e00", "#fdf802",
                            "#e5bc00", "#fd9500", "#fd0000", "#d40000", "#bc0000", "#f800fd", "#9854c6"])


# ---------------------------------------------------------------- data access

def read_index(url):
    """[(offset, description)] for every message in a GRIB file."""
    entries = []
    for line in session.get(url + ".idx", timeout=60).text.strip().splitlines():
        parts = line.split(":")
        entries.append((int(parts[1]), ":".join(parts[3:])))
    return entries


def byte_range(index, match, url, nth=0):
    """Byte range of the first message matching (or the nth, counting from 0, when several share a
    description). A match ending in "$" must equal the whole description, which keeps NBM's probability and
    percentile fields from matching the deterministic one."""
    exact = match.endswith("$")
    want = match[:-1].rstrip(":")
    for i, (start, desc) in enumerate(index):
        if (desc.rstrip(":") == want) if exact else (match in desc):
            if nth:
                nth -= 1
                continue
            end = index[i + 1][0] - 1 if i + 1 < len(index) else ""
            return start, end
    raise KeyError(f"{match!r} not in {url.rsplit('/', 1)[-1]}")


def decode(content, subset=None, step=1):
    """GRIB message bytes -> DataArray (a lat/lon box of a regular grid, or a thinned projected grid)."""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        da = ds[list(ds.data_vars)[0]]
        if subset:
            da = da.sel(latitude=subset[0], longitude=subset[1])
        elif step > 1:
            da = da.isel(y=slice(None, None, step), x=slice(None, None, step))
        # Extra time coords make MetPy warn; the run and valid times are tracked separately.
        da = da.load().drop_vars(["time", "step", "valid_time"], errors="ignore")
    finally:
        os.remove(path)
    return da


def get_fields(url, wanted, **kw):
    """Download {name: index match} fields from one file; a match can also be (match, n) for the nth of
    several messages with the same description. Downloads run in parallel; decoding stays on one thread
    because ecCodes isn't guaranteed to be thread-safe."""
    index = read_index(url)

    def fetch(match):
        match, nth = (match, 0) if isinstance(match, str) else match
        start, end = byte_range(index, match, url, nth)
        resp = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
        resp.raise_for_status()
        return resp.content

    with ThreadPoolExecutor(max_workers=12) as pool:
        blobs = dict(zip(wanted, pool.map(fetch, wanted.values())))
    return {name: decode(blob, **kw) for name, blob in blobs.items()}


def latest_cycle(url_for, last_hour, every=6, index_suffix=".idx"):
    """Newest cycle (00/06/12/18Z, or 00/12Z with every=12) whose last forecast hour has been posted."""
    now = datetime.now(timezone.utc)
    cycle = now.replace(hour=now.hour // every * every, minute=0, second=0, microsecond=0)
    for _ in range(8):
        if session.head(index_url(url_for(cycle, last_hour), index_suffix), timeout=30).status_code == 200:
            return cycle
        cycle -= timedelta(hours=every)
    raise RuntimeError("No complete cycle found in the last two days")


def index_url(url, suffix):
    return url + suffix if suffix == ".idx" else url.removesuffix(".grib2") + suffix


def get_retry(url, tries=7, **kw):
    """GET with a few retries; the ECMWF bucket answers bursts with 503 SlowDown."""
    for i in range(tries):
        resp = session.get(url, timeout=120, **kw)
        if resp.status_code not in (429, 500, 503) or i == tries - 1:
            resp.raise_for_status()
            return resp
        time.sleep(min(2 ** (i + 1), 30))


# ---------------------------------------------------------------- drawing

def lonlat(da):
    """2-D (x, y) in Mercator radians for a field on a regular or projected grid."""
    if da.latitude.ndim == 1:
        lon, lat = np.meshgrid(da.longitude.values, da.latitude.values)
    else:
        lon, lat = da.longitude.values, da.latitude.values
    lon = (lon + 180) % 360 - 180
    return np.radians(lon), merc(lat)


def new_overlay():
    fig = plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=100)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(X0, X1)
    ax.set_ylim(Y0, Y1)
    ax.axis("off")
    return fig, ax


def save(fig, path):
    """Transparent PNG, reduced to a palette image (a few hundred KB → about a quarter of that)."""
    buf = io.BytesIO()
    fig.savefig(buf, dpi=100, transparent=True)
    plt.close(fig)
    buf.seek(0)
    Image.open(buf).convert("RGBA").quantize(colors=200, method=Image.Quantize.FASTOCTREE).save(path, optimize=True)


class Canvas:
    """The overlays of one product at one forecast hour. canvas("mslp") is the axes of the product's "mslp"
    layer, drawn and saved as its own transparent image; canvas() is the single overlay of a product without
    layers. json_path is where a product can write extra data for the hour (the Surface map's station plots)."""

    def __init__(self, json_path):
        self.figs, self.json_path = {}, json_path

    def __call__(self, layer=None):
        if layer not in self.figs:
            self.figs[layer] = new_overlay()
        return self.figs[layer][1]


def layer(lid, name, legend=None, key=None, line=None):
    """One part of a product that the page can turn on and off, with its share of the legend: a color scale
    (legend), swatches (key: [[color, label]]) or a note on what its lines mean (line)."""
    out = {"id": lid, "name": name}
    if legend:
        out["legend"] = legend
    if key:
        out["key"] = key
    if line:
        out["line"] = line
    return out


def product(pid, name, draw, legend, note=None, layers=None):
    return {"id": pid, "name": name, "draw": draw, "legend": legend, "note": note, "layers": layers}


def shade(ax, x, y, values, scale, **kw):
    cmap, norm = scale
    return ax.contourf(x, y, values, levels=norm.boundaries, cmap=cmap, norm=norm, zorder=2, **kw)


def contours(ax, x, y, values, levels, color="#ffffff", width=0.9, dashed=False):
    cs = ax.contour(x, y, values, levels=levels, colors=color, linewidths=width,
                    linestyles="--" if dashed else "-", zorder=4)
    ax.clabel(cs, fmt="%d", fontsize=8, inline=True, inline_spacing=2)


def barbs(ax, x, y, u, v, every=10, color="#dfe6ff"):
    """Earth-relative winds (m/s): Mercator keeps angles, so barbs can be drawn straight on."""
    s = (slice(None, None, every), slice(None, None, every))
    ax.barbs(x[s], y[s], np.asarray(u)[s] * 1.94384, np.asarray(v)[s] * 1.94384,  # m/s -> kt
             length=5, linewidth=0.55, color=color, zorder=5)


def with_units(da, unit):
    da = da.copy()
    da.attrs["units"] = unit
    return da.metpy.quantify()


# ---------------------------------------------------------------- GFS

GFS_BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GFS_SUBSET = (slice(60, 15), slice(225, 305))   # GFS longitudes run 0-360
TURB_LAYERS = [(400, 300), (300, 250), (250, 200)]   # (lower, upper) mb
ICING_LEVELS = [900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400]


def gfs_url(cycle, fhr):
    return f"{GFS_BUCKET}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{fhr:03d}"


def gfs_fields(fhr):
    wanted = {
        "mslp": "PRMSL:mean sea level",
        "apcp": f"APCP:surface:{fhr - 6}-{fhr} hour acc",
        "psfc": "PRES:surface:",
        "zsfc": "HGT:surface:",
        "tcdc": f"TCDC:entire atmosphere:{fhr} hour fcst",
        "ceil": "HGT:cloud ceiling:",
        "frzl": "HGT:0C isotherm:",
        "t2m": "TMP:2 m above ground:",
        "d2m": "DPT:2 m above ground:",
        "u10": "UGRD:10 m above ground:",
        "v10": "VGRD:10 m above ground:",
        "gust": f"GUST:surface:{fhr} hour fcst",
        "prate": f"PRATE:surface:{fhr} hour fcst",   # the rate at the valid time, not the 6-hour average
    }
    for var in ("CRAIN", "CSNOW", "CICEP", "CFRZR"):
        wanted[var.lower()] = f"{var}:surface:{fhr} hour fcst"
    wanted["pwat"] = "PWAT:entire atmosphere"
    wanted["z500"] = "HGT:500 mb:"
    for lev in (850, 500):
        wanted[f"u{lev}"], wanted[f"v{lev}"] = f"UGRD:{lev} mb:", f"VGRD:{lev} mb:"
    for lev in {700, 200} | {lev for pair in TURB_LAYERS for lev in pair}:
        for var, key in (("HGT", "z"), ("UGRD", "u"), ("VGRD", "v")):
            wanted[f"{key}{lev}"] = f"{var}:{lev} mb:"
    for lev in ICING_LEVELS:
        for var, key in (("TMP", "t"), ("RH", "rh"), ("CLMR", "clw")):
            wanted[f"{key}{lev}"] = f"{var}:{lev} mb:"
    return wanted


def gfs_precip(f, canvas):
    x, y = lonlat(f["mslp"])
    shade(canvas("precip"), x, y, f["apcp"].values / 25.4, PRECIP)   # mm -> in
    contours(canvas("mslp"), x, y, gaussian_filter(f["mslp"].values / 100, sigma=1.5), np.arange(940, 1061, 4))


def gfs_vort700(f, canvas):
    u, v, hgt = f["u700"], f["v700"], f["z700"]
    x, y = lonlat(hgt)
    vort = mpcalc.vorticity(u.metpy.quantify(), v.metpy.quantify())
    vort = gaussian_filter(vort.metpy.dequantify().values * 1e5, sigma=2)
    shade(canvas("vort"), x, y, vort, VORT, extend="both")
    contours(canvas("heights"), x, y, gaussian_filter(hgt.values / 10, sigma=1.5), np.arange(240, 330, 3))
    barbs(canvas("barbs"), x, y, u, v)


def gfs_wind200(f, canvas):
    u, v, hgt = f["u200"], f["v200"], f["z200"]
    x, y = lonlat(hgt)
    shade(canvas("speed"), x, y, np.hypot(u.values, v.values) * 1.94384, JET, alpha=0.9, extend="max")
    contours(canvas("heights"), x, y, gaussian_filter(hgt.values / 10, sigma=1.5), np.arange(1080, 1260, 12))
    barbs(canvas("barbs"), x, y, u, v)


def gfs_clouds(f, canvas):
    x, y = lonlat(f["tcdc"])
    ceil = f["ceil"].values
    # Ceiling is a height above sea level; 20,000 m means no ceiling.
    ceil_ft = np.where(ceil < 15000, (ceil - f["zsfc"].values) * 3.28084, np.nan)
    shade(canvas("cloud"), x, y, f["tcdc"].values, CLOUD)
    # Each flight category is its own layer: LIFR below 500 ft, IFR 500-999 ft, MVFR 1,000-3,000 ft.
    lower = 0
    for (upper, _, color), lid in zip(CEILING_CATS, CEILING_LAYERS):
        if ((ceil_ft >= lower) & (ceil_ft < upper)).any():   # an empty layer is still saved, blank
            canvas(lid).contourf(x, y, ceil_ft, levels=[lower, upper], colors=[color], alpha=0.85, zorder=2.5)
        lower = upper


def ellrod_ti2(f):
    """Ellrod-Knapp TI2 (10^-7 s^-2): vertical shear x (deformation + convergence), the largest of
    the layers in TURB_LAYERS."""
    best = None
    for lo, up in TURB_LAYERS:
        du = f[f"u{up}"].values - f[f"u{lo}"].values
        dv = f[f"v{up}"].values - f[f"v{lo}"].values
        dz = f[f"z{up}"].values - f[f"z{lo}"].values
        vws = np.hypot(du, dv) / dz
        um = with_units((f[f"u{up}"] + f[f"u{lo}"]) / 2, "m/s")
        vm = with_units((f[f"v{up}"] + f[f"v{lo}"]) / 2, "m/s")
        deform = mpcalc.total_deformation(um, vm).metpy.dequantify().values
        conv = -mpcalc.divergence(um, vm).metpy.dequantify().values
        ti2 = vws * (deform + conv) * 1e7
        best = ti2 if best is None else np.maximum(best, ti2)
    return gaussian_filter(np.clip(best, 0, None), sigma=1)


def gfs_turb(f, canvas):
    x, y = lonlat(f["u250"])
    jet = gaussian_filter(np.hypot(f["u250"].values, f["v250"].values) * 1.94384, sigma=1)
    shade(canvas("ti2"), x, y, ellrod_ti2(f), TURB, extend="max")
    contours(canvas("isotachs"), x, y, jet, [70, 90, 110, 130, 150, 170], width=0.8)


def icing_potential(f):
    """Max over ICING_LEVELS of temperature interest x moisture interest (0-1)."""
    psfc = f["psfc"].values / 100  # Pa -> hPa
    best = np.zeros_like(psfc)
    for lev in ICING_LEVELS:
        t_c = f[f"t{lev}"].values - 273.15
        # Supercooled liquid is most likely from about -4 to -14 C.
        t_score = np.interp(t_c, [-22, -14, -4, 0], [0, 1, 1, 0], left=0, right=0)
        clw_score = np.interp(f[f"clw{lev}"].values * 1000, [0.01, 0.2], [0, 1])   # kg/kg -> g/kg
        # Humidity alone (no model cloud water) counts for less.
        rh_score = 0.6 * np.interp(f[f"rh{lev}"].values, [70, 95], [0, 1])
        pot = t_score * np.maximum(clw_score, rh_score)
        pot[psfc < lev] = 0  # level is below ground
        best = np.maximum(best, pot)
    return gaussian_filter(best, sigma=1)


def gfs_icing(f, canvas):
    x, y = lonlat(f["psfc"])
    shade(canvas("potential"), x, y, icing_potential(f), ICE)
    contours(canvas("frzl"), x, y, gaussian_filter(f["frzl"].values * 3.28084 / 1000, sigma=1.5), np.arange(2, 17, 2),
             color="#c9d2f0", width=0.7, dashed=True)


# ---------------------------------------------------------------- NBM

NBM_BUCKET = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"


def nbm_url(cycle, fhr):
    return f"{NBM_BUCKET}/blend.{cycle:%Y%m%d}/{cycle:%H}/core/blend.t{cycle:%H}z.core.f{fhr:03d}.co.grib2"


def nbm_fields(fhr):
    return {
        "t2m": f"TMP:2 m above ground:{fhr} hour fcst$",
        "apcp": f"APCP:surface:{fhr - 6}-{fhr} hour acc fcst$",
        "snow": f"ASNOW:surface:{fhr - 6}-{fhr} hour acc fcst$",
        "gust": f"GUST:10 m above ground:{fhr} hour fcst$",
        "sky": f"TCDC:surface:{fhr} hour fcst$",
        **(nbm_cloud_layer_fields(fhr) if fhr <= NBM_LAYER_HOURS else {}),
    }


# The NBM gives the base, coverage and top of up to three cloud layers, lowest first, out to F036. Their
# index entries say "reserved" because the levels are NCEP local codes (195, 196, 197), so the three are told
# apart by order. Heights are in metres above ground. A layer that isn't there has coverage 0 and is filled
# with a base of 25,000 ft and a top of 45,000 ft (7620.5 and 13716 m); tops are capped at 45,000 ft.
NBM_LAYER_HOURS = 36


def nbm_cloud_layer_fields(fhr):
    w = {}
    for n in range(3):
        for var, key in (("CDCB", "base"), ("TCDC", "cover"), ("CDCTOP", "top")):
            w[f"{key}{n + 1}"] = (f"{var}:reserved:{fhr} hour fcst$", n)
    w["retop"] = f"RETOP:cloud top:{fhr} hour fcst$"
    return w


def echo_top_lines(ax, x, y, tops_kft):
    """Labelled contours of the radar echo top: where the model's storms reach 25, 35 and 45 thousand ft."""
    halo = [pe.withStroke(linewidth=2.4, foreground="#0b1022")]
    cs = ax.contour(x, y, gaussian_filter(np.nan_to_num(tops_kft), sigma=1), levels=ECHO_TOPS,
                    colors=["#ffd166", "#ff9f43", "#ff453a"], linewidths=1.1, zorder=4)
    cs.set(path_effects=halo)
    for t in ax.clabel(cs, fmt="%d", fontsize=8, inline=True, inline_spacing=2):
        t.set_path_effects(halo)


def nbm_cloud_tops(f, canvas):
    """The top of the highest cloud layer with any coverage, and echo tops. Past F036 the layers are blank."""
    if "top1" not in f:
        return
    x, y = lonlat(f["top1"])
    tops = np.full(f["top1"].shape, np.nan)
    for n in (1, 2, 3):
        base, top = f[f"base{n}"].values, f[f"top{n}"].values
        # Some thin layers come with coverage but only the fill values for base and top; their height is unknown.
        filled = (np.abs(base - 7620.5) < 2) & (top > 13700)
        here = (f[f"cover{n}"].values > 0) & ~filled
        tops = np.where(here, np.fmax(tops, top), tops)
    shade(canvas("tops"), x, y, np.minimum(tops * FT / 1000, TOPS_LEVELS[-1] - 0.01), TOPS)
    echo = f["retop"].values.astype(float)
    echo[(echo <= 0) | (echo > 80000)] = np.nan   # 88888 means no echo
    echo_top_lines(canvas("echo"), x, y, echo * FT / 1000)


def simple(field, scale, convert=lambda v: v):
    def draw(f, canvas):
        x, y = lonlat(f[field])
        shade(canvas(), x, y, convert(f[field].values), scale, extend="max" if scale is not TEMP else "both")
    return draw


K_TO_F = lambda v: (v - 273.15) * 9 / 5 + 32
MS_TO_MPH = lambda v: v * 2.23694


# ---------------------------------------------------------------- HRRR

HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
HRRR_CONE = np.sin(np.radians(38.5))


def hrrr_url(cycle, fhr):
    return f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf{fhr:02d}.grib2"


def hrrr_fields(fhr):
    return {
        "refc": f"REFC:entire atmosphere:{fhr} hour fcst$",
        "t2m": f"TMP:2 m above ground:{fhr} hour fcst$",
        "gust": f"GUST:surface:{fhr} hour fcst$",
        "apcp": f"APCP:surface:{fhr - 1}-{fhr} hour acc fcst$",
        "cape": f"CAPE:surface:{fhr} hour fcst$",
        "ushr": f"VUCSH:0-6000 m above ground:{fhr} hour fcst$",
        "vshr": f"VVCSH:0-6000 m above ground:{fhr} hour fcst$",
        "uh": f"MXUPHL:5000-2000 m above ground:{fhr - 1}-{fhr} hour max fcst$",
        "ctop": f"HGT:cloud top:{fhr} hour fcst$",
        "retop": f"RETOP:cloud top:{fhr} hour fcst$",
        "ustm": f"USTM:0-6000 m above ground:{fhr} hour fcst$",
        "vstm": f"VSTM:0-6000 m above ground:{fhr} hour fcst$",
        "tcc": f"TCDC:entire atmosphere:{fhr} hour fcst$",
        "mslp": f"MSLMA:mean sea level:{fhr} hour fcst$",
        "d2m": f"DPT:2 m above ground:{fhr} hour fcst$",
        "u10": f"UGRD:10 m above ground:{fhr} hour fcst$",
        "v10": f"VGRD:10 m above ground:{fhr} hour fcst$",
        "prate": f"PRATE:surface:{fhr} hour fcst$",
        **{var.lower(): f"{var}:surface:{fhr} hour fcst$" for var in ("CRAIN", "CSNOW", "CICEP", "CFRZR")},
    }


def hrrr_earth_wind(lon, u, v):
    """HRRR winds are relative to its Lambert conformal grid (true at 38.5°N, centered on 97.5°W); turn them to
    true north before plotting."""
    a = np.radians(HRRR_CONE * ((lon + 180) % 360 - 180 + 97.5))
    return np.cos(a) * u + np.sin(a) * v, -np.sin(a) * u + np.cos(a) * v


def hrrr_cloud_tops(f, canvas):
    """Cloud top height above sea level, echo tops, and the model's storm motion."""
    x, y = lonlat(f["ctop"])
    tops = f["ctop"].values * FT / 1000   # missing where there's no cloud
    shade(canvas("tops"), x, y, np.minimum(tops, TOPS_LEVELS[-1] - 0.01), TOPS)
    echo = f["retop"].values.astype(float)
    echo[echo <= 0] = np.nan   # -999 means no echo
    echo_top_lines(canvas("echo"), x, y, echo * FT / 1000)
    u, v = hrrr_earth_wind(f["ustm"].longitude.values, f["ustm"].values, f["vstm"].values)
    barbs(canvas("motion"), x, y, u, v, every=35, color="#b8ffd9")


def hrrr_severe(f, canvas):
    """Surface-based CAPE, with 0–6 km bulk shear contoured. HRRR labels the shear 1/s but stores m/s."""
    x, y = lonlat(f["cape"])
    shear = gaussian_filter(np.hypot(f["ushr"].values, f["vshr"].values) * 1.94384, sigma=2)
    shade(canvas("cape"), x, y, f["cape"].values, CAPE, extend="max")
    contours(canvas("shear"), x, y, shear, [30, 40, 50, 60, 70], width=0.8)


# ---------------------------------------------------------------- ECMWF

ECMWF_BUCKET = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
ECMWF_SUBSET = (slice(60, 15), slice(-135, -55))   # open data longitudes run -180-180


def ecmwf_url(cycle, fhr):
    return f"{ECMWF_BUCKET}/{cycle:%Y%m%d}/{cycle:%H}z/ifs/0p25/oper/{cycle:%Y%m%d%H}0000-{fhr}h-oper-fc.grib2"


def ecmwf_fields(fhr):
    """(param, level) pairs. Precipitation is accumulated from the start of the run, so the 6-hour
    total needs the previous step's file too ("tp_prev")."""
    wanted = {"mslp": ("msl", None), "tp": ("tp", None), "t2m": ("2t", None), "d2m": ("2d", None),
              "u10": ("10u", None), "v10": ("10v", None), "gust": [("10fg", None), ("10fg3", None)], "tcc": ("tcc", None),
              "ptype": ("ptype", None), "tprate": ("tprate", None), "pwat": ("tcwv", None)}
    for lev in (700, 500, 200):
        for var, key in (("gh", "z"), ("u", "u"), ("v", "v")):
            wanted[f"{key}{lev}"] = (var, str(lev))
    for lev in (850, 250):
        wanted[f"u{lev}"], wanted[f"v{lev}"] = ("u", str(lev)), ("v", str(lev))
    return wanted


def ecmwf_get(url, wanted, subset=None):
    """Fields from ECMWF open data, located with the JSON-lines .index next to each file."""
    def index(u):
        rows = [json.loads(line) for line in get_retry(index_url(u, ".index")).text.splitlines() if line.startswith("{")]
        return {(r["param"], r.get("levelist")): (r["_offset"], r["_length"]) for r in rows}

    def fetch(u, idx, key):
        off, length = idx[key]
        return get_retry(u, headers={"Range": f"bytes={off}-{off + length - 1}"}).content

    idx = index(url)
    # A list gives alternatives: the gust is the last hour's (10fg) or, on 3-hourly steps, the last 3 hours' (10fg3).
    wanted = {name: next((k for k in key if k in idx), key[0]) if isinstance(key, list) else key for name, key in wanted.items()}
    with ThreadPoolExecutor(max_workers=3) as pool:   # the bucket rate-limits more than a few at once
        blobs = dict(zip(wanted, pool.map(lambda key: fetch(url, idx, key), wanted.values())))
    f = {name: decode(blob, subset=subset) for name, blob in blobs.items()}
    fhr = int(re.search(r"-(\d+)h-oper", url).group(1))
    prev = url.replace(f"-{fhr}h-oper", f"-{fhr - 6}h-oper")
    if fhr - 6 > 0:
        tp_prev = decode(fetch(prev, index(prev), ("tp", None)), subset=subset)
    else:
        tp_prev = f["tp"] * 0
    f["apcp"] = (f["tp"] - tp_prev).clip(min=0) * 1000   # m -> mm, as the GFS drawing expects
    # Geopotential height arrives in metres (gh), the same as GFS HGT.
    return f


# ---------------------------------------------------------------- Surface map
# Cloud cover, precipitation rate by type and sea-level pressure in the overlay, plus forecast station plots.
# The station plots are drawn by the page from a small JSON file per hour, so they stay sharp at every zoom
# and thin out to fit. Each model's fields are first put in common units by a "prep" function:
#   tcc %, rate mm/h, ptype (0 none, 1 rain, 2 snow, 3 ice pellets, 4 freezing rain), mslp hPa,
#   t2m and d2m K, u10/v10 earth-relative m/s, gust m/s

STATIONS = json.loads((OUT / "stations.json").read_text()) if (OUT / "stations.json").exists() else []


def unit_xyz(lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def nearest_points(da):
    """Flat grid index of the point nearest each station, or -1 where the grid doesn't reach it."""
    if da.latitude.ndim == 1:
        lon, lat = np.meshgrid(da.longitude.values, da.latitude.values)
    else:
        lon, lat = da.longitude.values, da.latitude.values
    tree = cKDTree(unit_xyz(lat.ravel(), (lon.ravel() + 180) % 360 - 180))
    dist, idx = tree.query(unit_xyz([s[1] for s in STATIONS], [s[2] for s in STATIONS]))
    return np.where(dist < np.radians(0.3), idx, -1)


def noaa_ptype(f):
    """NOAA's yes/no precipitation-type flags, most hazardous first when more than one is set."""
    code = np.zeros(f["crain"].shape, dtype=int)
    for key, c in (("crain", 1), ("csnow", 2), ("cicep", 3), ("cfrzr", 4)):
        code = np.where(f[key].values >= 0.5, c, code)
    return code


def gfs_surface_prep(f):
    return {"grid": f["mslp"], "tcc": f["tcdc"].values, "rate": f["prate"].values * 3600, "ptype": noaa_ptype(f),
            "mslp": gaussian_filter(f["mslp"].values / 100, sigma=1.5), "t2m": f["t2m"].values, "d2m": f["d2m"].values,
            "u10": f["u10"].values, "v10": f["v10"].values, "gust": f["gust"].values}


def hrrr_surface_prep(f):
    u, v = hrrr_earth_wind(f["u10"].longitude.values, f["u10"].values, f["v10"].values)
    return {"grid": f["t2m"], "tcc": f["tcc"].values, "rate": f["prate"].values * 3600, "ptype": noaa_ptype(f),
            "mslp": gaussian_filter(f["mslp"].values / 100, sigma=3), "t2m": f["t2m"].values, "d2m": f["d2m"].values,
            "u10": u, "v10": v, "gust": f["gust"].values}


# ECMWF precipitation type codes -> ours. Wet snow and rain-snow mix count as snow; freezing drizzle as
# freezing rain.
ECMWF_PTYPE = {1: 1, 3: 4, 5: 2, 6: 2, 7: 2, 8: 3, 12: 4}


def ecmwf_surface_prep(f):
    tcc = f["tcc"].values
    ptype = np.zeros(tcc.shape, dtype=int)
    for theirs, ours in ECMWF_PTYPE.items():
        ptype[np.round(np.nan_to_num(f["ptype"].values)) == theirs] = ours
    return {"grid": f["mslp"], "tcc": tcc * 100 if np.nanmax(tcc) <= 1.01 else tcc, "rate": f["tprate"].values * 3600,
            "ptype": ptype, "mslp": gaussian_filter(f["mslp"].values / 100, sigma=1.5), "t2m": f["t2m"].values,
            "d2m": f["d2m"].values, "u10": f["u10"].values, "v10": f["v10"].values, "gust": f["gust"].values}


def surface(prep):
    def draw(f, canvas):
        s = prep(f)
        x, y = lonlat(s["grid"])
        rate = s["rate"] / 25.4   # mm/h -> in/h
        # Wet spots with no type flagged (rare, at type boundaries) are drawn as rain.
        ptype = np.where((s["ptype"] == 0) & (rate >= RATE_LEVELS[0]), 1, s["ptype"])
        shade(canvas("clouds"), x, y, s["tcc"], SKY)
        rate = np.minimum(rate, RATE_LEVELS[-1] * 0.999)   # the top bin also holds anything heavier
        ax = canvas("precip")
        for code, _, colors in PTYPES:
            if (ptype == code).any():
                shade(ax, x, y, np.where(ptype == code, rate, np.nan), binned(RATE_LEVELS, colors))
        # White lines with a dark edge read over both clear (dark) and overcast (light gray) areas.
        ax = canvas("mslp")
        halo = [pe.withStroke(linewidth=2.6, foreground="#0b1022")]
        cs = ax.contour(x, y, s["mslp"], levels=np.arange(940, 1061, 4), colors="#ffffff", linewidths=1, zorder=4)
        cs.set(path_effects=halo)
        for t in ax.clabel(cs, fmt="%d", fontsize=8, inline=True, inline_spacing=2):
            t.set_path_effects(halo)
        write_stations(s, ptype, s["rate"] / 25.4, canvas.json_path)
    return draw


def write_stations(s, ptype, rate, path):
    """One row per station in site/stations.json: [temp °F, dew point °F, pressure (tenths of hPa), sky %,
    wind direction °, wind kt, gust kt, precip type, rate (hundredths of in/h)], or null off the grid."""
    idx = nearest_points(s["grid"])
    pick = lambda a: np.asarray(a).ravel()[np.maximum(idx, 0)]
    t, d = K_TO_F(pick(s["t2m"])), K_TO_F(pick(s["d2m"]))
    u, v = pick(s["u10"]), pick(s["v10"])
    spd = np.hypot(u, v) * 1.94384
    wdir = (270 - np.degrees(np.arctan2(v, u))) % 360
    wdir = np.where(np.round(wdir / 10) == 0, 360, np.round(wdir / 10) * 10)
    cols = [t, d, pick(s["mslp"]) * 10, pick(s["tcc"]), wdir, spd, pick(s["gust"]) * 1.94384, pick(ptype), pick(rate) * 100]
    rows = []
    for i in range(len(STATIONS)):
        vals = [c[i] for c in cols]
        rows.append(None if idx[i] < 0 or not np.isfinite(vals).all() else [int(round(float(v))) for v in vals])
    path.write_text(json.dumps(rows, separators=(",", ":")))


SURFACE_NOTE = ("Gray shading is cloud cover; color is the precipitation rate at the valid time, by type. "
                "Station plots: temperature (red) and dew point (green) in °F, sea-level pressure (purple, last three "
                "digits in tenths of hPa, so 132 is 1013.2), sky cover in the circle, wind barbs in knots, and gusts "
                "(G, orange) where they reach 15 kt. Hover a plot for details; zoom in for more stations.")


def surface_legend():
    leg = legend("Precipitation rate (in/h) by type", RATE_LEVELS, binned(RATE_LEVELS, PTYPES[0][2])[0],
                 lines="White lines: sea-level pressure, every 4 hPa")
    leg["ramps"] = [[name, colors] for _, name, colors in PTYPES]
    leg["under"] = legend("Cloud cover (%)", SKY_LEVELS, SKY[0])
    leg["stations"] = True
    return leg


def surface_layers():
    """Bottom to top. The station plots, drawn by the page, are one more layer on top."""
    rate = legend("Precipitation rate (in/h) by type", RATE_LEVELS, binned(RATE_LEVELS, PTYPES[0][2])[0])
    rate["ramps"] = [[name, colors] for _, name, colors in PTYPES]
    return [layer("clouds", "Cloud cover", legend("Cloud cover (%)", SKY_LEVELS, SKY[0])),
            layer("precip", "Precipitation rate", rate),
            layer("mslp", "Sea-level pressure", line="White lines: sea-level pressure, every 4 hPa")]


# ---------------------------------------------------------------- Upper air map
# A composite of the main ingredients: precipitable water, the 250 mb jet and smoothed 500 mb vorticity in
# color, with 500 mb heights (red or blue where they sit 2 standard deviations above or below normal for the
# date), 850 mb wind arrows and sea-level pressure with its highs and lows. Each model supplies the common
# fields pwat (kg/m² = mm), u/v250, z500, u/v500, u/v850 (m/s, earth-relative) and mslp (Pa).

PWAT_LEVELS = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75]   # in
PWAT = ramp(PWAT_LEVELS, [(0.75, "#24452e"), (1.5, "#3d8a4f"), (2.25, "#5fc46a"), (2.75, "#b4f0a0")])
JET250_LEVELS = list(range(80, 221, 20))   # kt
JET250 = binned(JET250_LEVELS, ["#9fd6f5", "#5fb0ef", "#2f6fe0", "#7f6cf0", "#a34ae8", "#d040e0", "#f0b0ec"])
VORT500_LEVELS = [4, 8, 12, 16, 20, 24, 28]   # 10⁻⁵ s⁻¹, cyclonic only
VORT500 = ramp(VORT500_LEVELS, [(4, "#f6e33c"), (12, "#f5a33a"), (20, "#e8603a"), (28, "#c8326e")])
ANOM_HIGH, ANOM_LOW = "#ff5a4f", "#4aa8ff"   # heights at least 2σ above / below normal

CLIMATE = np.load(Path(__file__).parent / "z500_climate.npz") if (Path(__file__).parent / "z500_climate.npz").exists() else None


def z500_anomaly(z, valid):
    """Normalized 500 mb height anomaly (standard deviations from the 1991–2020 normal for the date), from
    the harmonic fits in z500_climate.npz (see make_climatology.py), interpolated to the model grid."""
    from scipy.interpolate import RegularGridInterpolator
    from make_climatology import design
    X = design([valid.timetuple().tm_yday])[0]
    lat, lon = CLIMATE["lat"][::-1], CLIMATE["lon"]   # the interpolator wants increasing axes
    mean = np.tensordot(X, CLIMATE["mean"], 1)[::-1]
    std = np.sqrt(np.maximum(np.tensordot(X, CLIMATE["var"], 1), 1))[::-1]
    glon, glat = np.meshgrid((z.longitude.values + 180) % 360 - 180, z.latitude.values)
    pts = np.column_stack([glat.ravel(), glon.ravel()])
    at = lambda a: RegularGridInterpolator((lat, lon), a, bounds_error=False, fill_value=np.nan)(pts).reshape(glat.shape)
    return (z.values - at(mean)) / at(std)


def anomaly_lines(ax, cs, x, y, anom):
    """Redraw the parts of each height contour where the anomaly reaches ±2σ, in red or blue."""
    from scipy.interpolate import RegularGridInterpolator
    from matplotlib.collections import LineCollection
    # Interpolate on the grid's own (x, y): rows and columns of a regular lat/lon grid stay straight in Mercator.
    interp = RegularGridInterpolator((y[::-1, 0], x[0]), np.nan_to_num(anom)[::-1], bounds_error=False, fill_value=0)
    for limit, color in ((2, ANOM_HIGH), (-2, ANOM_LOW)):
        runs = []
        for seg in (s for level in cs.allsegs for s in level if len(s) > 1):
            hit = interp(seg[:, ::-1]) * np.sign(limit) >= abs(limit)
            # Consecutive vertices in the anomaly area form one run; each run is a separate line.
            edges = np.flatnonzero(np.diff(np.r_[0, hit.astype(int), 0]))
            runs += [seg[a:b] for a, b in zip(edges[::2], edges[1::2]) if b - a > 1]
        if runs:
            ax.add_collection(LineCollection(runs, colors=color, linewidths=2.6, zorder=5.5, capstyle="round",
                                             path_effects=[pe.withStroke(linewidth=4.2, foreground="#0b1022")]))


def highs_and_lows(ax, x, y, mslp, size=60):
    """H and L with the central pressure at the highest and lowest point within about 15° (size grid points)."""
    from scipy.ndimage import maximum_filter, minimum_filter
    halo = [pe.withStroke(linewidth=3, foreground="#0b1022")]
    rows, cols = mslp.shape
    edge = 6   # skip extremes on the grid's edge; they are usually just the edge of a larger system
    for filt, letter, color in ((maximum_filter, "H", ANOM_LOW), (minimum_filter, "L", ANOM_HIGH)):
        extreme = filt(mslp, size=size, mode="nearest") == mslp
        for i, j in zip(*np.nonzero(extreme)):
            if edge <= i < rows - edge and edge <= j < cols - edge and X0 < x[i, j] < X1 and Y0 < y[i, j] < Y1:
                if letter == "H" and mslp[i, j] < 1012 or letter == "L" and mslp[i, j] > 1012:
                    continue   # flat areas give weak "extremes"
                ax.text(x[i, j], y[i, j], letter, color=color, fontsize=26, fontweight="bold", ha="center",
                        va="center", zorder=7, path_effects=halo)
                ax.text(x[i, j], y[i, j] - 0.016, f"{mslp[i, j]:.0f}", color=color, fontsize=13, fontweight="bold",
                        ha="center", va="top", zorder=7, path_effects=halo)


def upper_air(f, canvas):
    x, y = lonlat(f["z500"])
    shade(canvas("pwat"), x, y, f["pwat"].values / 25.4, PWAT, alpha=0.8)   # mm -> in
    jet = gaussian_filter(np.hypot(f["u250"].values, f["v250"].values) * 1.94384, sigma=1)
    shade(canvas("jet"), x, y, np.minimum(jet, JET250_LEVELS[-1] - 0.01), JET250, alpha=0.85)
    vort = mpcalc.vorticity(f["u500"].metpy.quantify(), f["v500"].metpy.quantify())
    vort = gaussian_filter(vort.metpy.dequantify().values * 1e5, sigma=4)
    shade(canvas("vort"), x, y, np.minimum(vort, VORT500_LEVELS[-1] - 0.01), VORT500, alpha=0.8)

    # Sea-level pressure: thin gray isobars under everything else.
    mslp = gaussian_filter(f["mslp"].values / 100, sigma=3)
    ax = canvas("mslp")
    cs = ax.contour(x, y, mslp, levels=np.arange(940, 1061, 4), colors="#a3abbd", linewidths=0.6, zorder=3)
    ax.clabel(cs, fmt="%d", fontsize=7, inline=True, inline_spacing=2)

    # 850 mb wind: equal-length arrows for direction, where it blows 20 kt or more (low-level jets, warm
    # and cold advection).
    u, v = f["u850"].values, f["v850"].values
    spd = np.hypot(u, v)
    s = (slice(4, None, 8), slice(4, None, 8))
    keep = spd[s] * 1.94384 >= 20
    canvas("wind850").quiver(x[s][keep], y[s][keep], (u / np.maximum(spd, 0.1))[s][keep], (v / np.maximum(spd, 0.1))[s][keep],
              color="#d6dcea", alpha=0.85, scale=90, width=0.0013, headwidth=4, headlength=4, headaxislength=3.6,
              pivot="middle", zorder=6)

    # 500 mb heights every 6 dam, white with a dark edge, then red or blue where they are unusually high or low.
    ax = canvas("z500")
    halo = [pe.withStroke(linewidth=3.2, foreground="#0b1022")]
    z = gaussian_filter(f["z500"].values / 10, sigma=2)
    cs = ax.contour(x, y, z, levels=np.arange(456, 613, 6), colors="#ffffff", linewidths=1.5, zorder=5)
    cs.set(path_effects=halo)
    for t in ax.clabel(cs, fmt="%d", fontsize=11, inline=True, inline_spacing=3):
        t.set_fontweight("bold")
        t.set_path_effects(halo)
    if CLIMATE is not None:   # after the labels, so the colored runs break around them too
        anomaly_lines(ax, cs, x, y, gaussian_filter(z500_anomaly(f["z500"], f["valid"]), sigma=2))

    highs_and_lows(canvas("hilo"), x, y, mslp)


def upper_air_legend():
    leg = legend("Precipitable water (in)", PWAT_LEVELS, PWAT[0],
                 lines="White lines: 500 mb heights (dam) · gray lines: sea-level pressure (hPa) · arrows: 850 mb wind of 20 kt or more")
    leg["more"] = [legend("250 mb wind (kt)", JET250_LEVELS, JET250[0]),
                   legend("500 mb vorticity (10⁻⁵ s⁻¹, smoothed)", VORT500_LEVELS, VORT500[0])]
    leg["extra"] = [[ANOM_HIGH, "Heights 2σ+ above normal"], [ANOM_LOW, "2σ+ below normal"]]
    return leg


def upper_air_layers():
    """Bottom to top, in the order the single composite used to stack them."""
    return [layer("pwat", "Precipitable water", legend("Precipitable water (in)", PWAT_LEVELS, PWAT[0])),
            layer("jet", "250 mb jet stream", legend("250 mb wind (kt)", JET250_LEVELS, JET250[0])),
            layer("vort", "500 mb vorticity", legend("500 mb vorticity (10⁻⁵ s⁻¹, smoothed)", VORT500_LEVELS, VORT500[0])),
            layer("mslp", "Sea-level pressure", line="Gray lines: sea-level pressure (hPa)"),
            layer("z500", "500 mb heights & anomalies", line="White lines: 500 mb heights (dam)",
                  key=[[ANOM_HIGH, "Heights 2σ+ above normal"], [ANOM_LOW, "2σ+ below normal"]]),
            layer("wind850", "850 mb wind arrows", line="Arrows: 850 mb wind of 20 kt or more"),
            layer("hilo", "Highs and lows", line="H and L: sea-level highs and lows (hPa)")]


UPPER_AIR_NOTE = ("Green is precipitable water, the moisture in the whole column; blue to pink is the 250 mb jet stream; "
                  "yellow to red is spin at 500 mb, which marks troughs and shortwaves that lift the air. "
                  "500 mb height lines turn red or blue where heights are at least two standard deviations above or "
                  "below the 1991–2020 normal for the date (NCEP/NCAR Reanalysis). Arrows show 850 mb wind direction "
                  "where it reaches 20 kt; H and L mark sea-level highs and lows with their central pressure.")


# ---------------------------------------------------------------- models

LINES_PRESSURE = "White lines: sea-level pressure, every 4 hPa"
ECHO_LINE = "Lines: echo tops at 25 (yellow), 35 (orange) and 45 (red) thousand ft"
TOPS_LEGEND = legend("Cloud top (thousands of ft above ground)", TOPS_LEVELS, TOPS[0], lines=ECHO_LINE)
TOPS_LEGEND_MSL = legend("Cloud top (thousands of ft above sea level)", TOPS_LEVELS, TOPS[0],
                         lines=ECHO_LINE + " · green barbs: storm motion (kt)")
CLOUDS_LEGEND = legend("Total cloud cover (%)", CLOUD_LEVELS, CLOUD[0], extra=[[c[2], c[1]] for c in CEILING_CATS])
CLOUDS_LAYERS = [layer("cloud", "Cloud cover", legend("Total cloud cover (%)", CLOUD_LEVELS, CLOUD[0]))] + [
    layer(lid, label.split(" ")[0] + " ceilings", key=[[color, label]])
    for lid, (_, label, color) in reversed(list(zip(CEILING_LAYERS, CEILING_CATS)))]   # MVFR, IFR, LIFR


def surface_product(prep):
    return product("surface", "Surface map", surface(prep), surface_legend(), SURFACE_NOTE, surface_layers())


UPPER_AIR = product("upper", "Upper air map", upper_air, upper_air_legend(), UPPER_AIR_NOTE, upper_air_layers())
PRECIP_MSLP = product("precip_mslp", "Precip & pressure", gfs_precip,
                      legend("6-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0], lines=LINES_PRESSURE),
                      layers=[layer("precip", "6-hour precipitation", legend("6-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0])),
                              layer("mslp", "Sea-level pressure", line=LINES_PRESSURE)])
VORT700 = product("vort700", "700 mb vorticity", gfs_vort700,
                  legend("Relative vorticity (10⁻⁵ s⁻¹)", VORT_LEVELS, VORT[0], lines="White lines: 700 mb heights (dam) · barbs: wind (kt)"),
                  layers=[layer("vort", "Vorticity", legend("Relative vorticity (10⁻⁵ s⁻¹)", VORT_LEVELS, VORT[0])),
                          layer("heights", "700 mb heights", line="White lines: 700 mb heights (dam)"),
                          layer("barbs", "Wind barbs", line="Barbs: wind (kt)")])
WIND200 = product("wind200", "200 mb jet stream", gfs_wind200,
                  legend("Wind speed (kt)", JET_LEVELS, JET[0], lines="White lines: 200 mb heights (dam) · barbs: wind (kt)"),
                  layers=[layer("speed", "Wind speed", legend("Wind speed (kt)", JET_LEVELS, JET[0])),
                          layer("heights", "200 mb heights", line="White lines: 200 mb heights (dam)"),
                          layer("barbs", "Wind barbs", line="Barbs: wind (kt)")])

MODELS = {
    "gfs": {
        "name": "GFS", "long": "GFS 0.25°", "source": "NOAA Global Forecast System via AWS Open Data",
        "url": gfs_url, "hours": list(range(6, 169, 6)), "fields": gfs_fields, "decode": {"subset": GFS_SUBSET},
        "products": [
            surface_product(gfs_surface_prep),
            UPPER_AIR,
            PRECIP_MSLP,
            VORT700,
            WIND200,
            product("clouds", "Clouds & ceilings", gfs_clouds, CLOUDS_LEGEND,
                    "Ceiling is the model's estimate of the lowest broken or overcast cloud base, in feet above ground.",
                    CLOUDS_LAYERS),
            product("turb", "Turbulence", gfs_turb,
                    legend("Ellrod TI2 (10⁻⁷ s⁻²) · light → severe", TURB_LEVELS, TURB[0], lines="White lines: 250 mb isotachs (kt)"),
                    "Clear-air turbulence potential from the Ellrod index (TI2), the highest value in the 400–200 mb "
                    "layer (about FL240–FL390). " + ESTIMATE_NOTE,
                    [layer("ti2", "Turbulence potential", legend("Ellrod TI2 (10⁻⁷ s⁻²) · light → severe", TURB_LEVELS, TURB[0])),
                     layer("isotachs", "250 mb isotachs", line="White lines: 250 mb isotachs (kt)")]),
            product("icing", "Icing", gfs_icing,
                    legend("Icing potential · low → high", ICE_LEVELS, ICE[0], lines="Dashed lines: freezing level (thousands of ft)"),
                    "Highest icing potential between 900 and 400 mb, from temperature (0 to −22 °C) and model cloud "
                    "liquid water or humidity. " + ESTIMATE_NOTE,
                    [layer("potential", "Icing potential", legend("Icing potential · low → high", ICE_LEVELS, ICE[0])),
                     layer("frzl", "Freezing level", line="Dashed lines: freezing level (thousands of ft)")]),
        ],
    },
    "nbm": {
        "name": "NBM", "long": "National Blend of Models", "source": "NOAA National Blend of Models via AWS Open Data",
        "url": nbm_url, "hours": list(range(6, 169, 6)), "fields": nbm_fields, "decode": {"step": 2},
        "products": [
            product("temp", "Temperature", simple("t2m", TEMP, K_TO_F), legend("2 m temperature (°F)", TEMP_LEVELS, TEMP[0])),
            product("precip", "6-hour precip", simple("apcp", PRECIP, lambda v: v / 25.4), legend("6-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0])),
            product("snow", "6-hour snow", simple("snow", SNOW, lambda v: v * 39.3701), legend("6-hour snowfall (in)", SNOW_LEVELS, SNOW[0])),
            product("gust", "Wind gusts", simple("gust", GUST, MS_TO_MPH), legend("Wind gust (mph)", GUST_LEVELS, GUST[0])),
            product("sky", "Sky cover", simple("sky", CLOUD), legend("Sky cover (%)", CLOUD_LEVELS, CLOUD[0])),
            product("ctop", "Cloud tops", nbm_cloud_tops, TOPS_LEGEND,
                    "Top of the highest cloud layer, in thousands of feet above ground (the HRRR's are above sea "
                    "level). Colored lines are echo tops: how high the radar echoes of the forecast storms reach. "
                    "The NBM's cloud layers only go out 36 hours, so later hours are blank.",
                    [layer("tops", "Cloud tops", legend(TOPS_LEGEND["label"], TOPS_LEVELS, TOPS[0])),
                     layer("echo", "Echo tops", line=ECHO_LINE)]),
        ],
        "note": "The National Blend of Models combines many models and is the starting point for National Weather Service forecasts.",
    },
    "hrrr": {
        "name": "HRRR", "long": "HRRR 3 km", "source": "NOAA High-Resolution Rapid Refresh via AWS Open Data",
        "url": hrrr_url, "hours": list(range(1, 49)), "fields": hrrr_fields, "decode": {"step": 2},
        "products": [
            surface_product(hrrr_surface_prep),
            product("refc", "Simulated radar", simple("refc", REFL), legend("Composite reflectivity (dBZ)", REFL_LEVELS, REFL[0])),
            product("temp", "Temperature", simple("t2m", TEMP, K_TO_F), legend("2 m temperature (°F)", TEMP_LEVELS, TEMP[0])),
            product("precip", "1-hour precip", simple("apcp", PRECIP, lambda v: v / 25.4), legend("1-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0])),
            product("gust", "Wind gusts", simple("gust", GUST, MS_TO_MPH), legend("Wind gust (mph)", GUST_LEVELS, GUST[0])),
            product("severe", "Instability & shear", hrrr_severe,
                    legend("Surface-based CAPE (J/kg)", CAPE_LEVELS, CAPE[0], lines="White lines: 0–6 km bulk shear (kt)"),
                    "Storms need instability (CAPE) to grow; 40 kt or more of deep-layer shear lets them organize into "
                    "supercells and lines. Ingredients, not a forecast of storms.",
                    [layer("cape", "CAPE", legend("Surface-based CAPE (J/kg)", CAPE_LEVELS, CAPE[0])),
                     layer("shear", "0–6 km shear", line="White lines: 0–6 km bulk shear (kt)")]),
            product("ctop", "Cloud tops", hrrr_cloud_tops, TOPS_LEGEND_MSL,
                    "Top of the highest cloud, in thousands of feet above sea level (roughly flight level). Colored "
                    "lines are echo tops: how high the radar echoes of the forecast storms reach. Barbs are the "
                    "model's storm motion (Bunkers right-mover, from the 0–6 km wind), in knots.",
                    [layer("tops", "Cloud tops", legend(TOPS_LEGEND_MSL["label"], TOPS_LEVELS, TOPS[0])),
                     layer("echo", "Echo tops", line=ECHO_LINE),
                     layer("motion", "Storm motion", line="Green barbs: storm motion (kt)")]),
            product("uh", "Updraft helicity", simple("uh", UH),
                    legend("2–5 km updraft helicity, hourly max (m²/s²)", UH_LEVELS, UH[0]),
                    "Tracks of rotating updrafts in the model's simulated storms. Values above about 75 often mark "
                    "supercells; the exact place and time will differ."),
        ],
        "note": "The HRRR is NOAA's hourly-updated 3 km model. Its 00, 06, 12 and 18Z runs go out 48 hours.",
    },
    "ecmwf": {
        "name": "ECMWF", "long": "ECMWF IFS 0.25°", "source": "ECMWF open data (CC BY 4.0) via AWS",
        "url": ecmwf_url, "hours": list(range(6, 169, 6)), "fields": ecmwf_fields, "decode": {"subset": ECMWF_SUBSET},
        "get": ecmwf_get, "every": 12, "index_suffix": ".index",
        "products": [
            surface_product(ecmwf_surface_prep),
            UPPER_AIR,
            PRECIP_MSLP,
            VORT700,
            WIND200,
            product("temp", "Temperature", simple("t2m", TEMP, K_TO_F), legend("2 m temperature (°F)", TEMP_LEVELS, TEMP[0])),
        ],
        "note": "The European Centre's IFS, often the most accurate global model in the medium range. "
                "Compare it with the GFS to see how much the two agree. Contains modified Copernicus/ECMWF data (CC BY 4.0).",
    },
}


def layer_file(pid, lid, fhr):
    return f"{pid}-{lid}_f{fhr:03d}.png" if lid else f"{pid}_f{fhr:03d}.png"


def draw_product(p, fields, img, fhr):
    canvas = Canvas(img / f"{p['id']}_f{fhr:03d}.json")
    for lay in p["layers"] or []:   # every layer gets an image, even one with nothing in it this hour
        canvas(lay["id"])
    p["draw"](fields, canvas)
    for lid, (fig, _) in canvas.figs.items():
        save(fig, img / layer_file(p["id"], lid, fhr))


def run(model_id):
    m = MODELS[model_id]
    hours = m["hours"][: int(os.environ["FW_HOURS"])] if os.environ.get("FW_HOURS") else m["hours"]
    img = OUT / "img" / model_id
    img.mkdir(parents=True, exist_ok=True)
    cycle = latest_cycle(m["url"], m["hours"][-1], m.get("every", 6), m.get("index_suffix", ".idx"))
    get = m.get("get", get_fields)
    print(f"{m['long']}: cycle {cycle:%Y-%m-%d %HZ}")
    done = []
    for fhr in hours:
        try:
            fields = get(m["url"](cycle, fhr), m["fields"](fhr), **m["decode"])
        except Exception as e:   # one bad hour shouldn't sink the run
            print(f"  F{fhr:03d} skipped: {e}")
            continue
        fields["valid"] = cycle + timedelta(hours=fhr)
        for p in m["products"]:
            draw_product(p, fields, img, fhr)
        done.append(fhr)
        print(f"  F{fhr:03d} done")
    if not done:
        raise SystemExit(f"{model_id}: no forecast hours drawn")
    info = {
        "id": model_id, "name": m["name"], "long": m["long"], "source": m["source"], "note": m.get("note"),
        "cycle": cycle.strftime("%Y-%m-%dT%HZ"), "init": cycle.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hours": done,
        "products": [{"id": p["id"], "name": p["name"], "legend": p["legend"],
                      **({"note": p["note"]} if p["note"] else {}), **({"layers": p["layers"]} if p["layers"] else {})}
                     for p in m["products"]],
    }
    (OUT / "models").mkdir(exist_ok=True)
    (OUT / "models" / f"{model_id}.json").write_text(json.dumps(info, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODELS:
        raise SystemExit(f"usage: make_plots.py {{{','.join(MODELS)}}}")
    run(sys.argv[1])
