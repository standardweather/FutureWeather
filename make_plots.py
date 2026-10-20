"""Download the latest model runs and draw transparent map overlays for the FutureWeather page.

Models (all from NOAA's open-data buckets on AWS; only the needed fields are downloaded, using the
byte offsets in each file's .idx index):
  gfs   Global Forecast System 0.25°, F006–F168 every 6 hours
  nbm   National Blend of Models (the basis of NWS forecasts), F006–F168 every 6 hours
  hrrr  High-Resolution Rapid Refresh 3 km, F001–F048 hourly

    python make_plots.py gfs        # or nbm, hrrr

Output, per model:
  site/img/<model>/<product>_f<hhh>.png   one transparent overlay per product per forecast hour
  site/models/<model>.json                run info, products and legends

Overlays are drawn in Web Mercator over BOUNDS, so the page lays them straight onto its Leaflet map.
merge_runs.py combines the per-model files into site/data.json.

FW_HOURS=2 limits each model to its first two forecast hours (for local testing).
"""

import io
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import metpy.calc as mpcalc
import numpy as np
import requests
import xarray as xr
from PIL import Image
from scipy.ndimage import gaussian_filter

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


def byte_range(index, match, url):
    """Byte range of the first message matching. A match ending in "$" must equal the whole description,
    which keeps NBM's probability and percentile fields from matching the deterministic one."""
    exact = match.endswith("$")
    want = match[:-1].rstrip(":")
    for i, (start, desc) in enumerate(index):
        if (desc.rstrip(":") == want) if exact else (match in desc):
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
    """Download {name: index match} fields from one file. Downloads run in parallel; decoding stays on
    one thread because ecCodes isn't guaranteed to be thread-safe."""
    index = read_index(url)

    def fetch(match):
        start, end = byte_range(index, match, url)
        resp = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
        resp.raise_for_status()
        return resp.content

    with ThreadPoolExecutor(max_workers=12) as pool:
        blobs = dict(zip(wanted, pool.map(fetch, wanted.values())))
    return {name: decode(blob, **kw) for name, blob in blobs.items()}


def latest_cycle(url_for, last_hour, every=6):
    """Newest cycle (00/06/12/18Z) whose last forecast hour has been posted."""
    now = datetime.now(timezone.utc)
    cycle = now.replace(hour=now.hour // every * every, minute=0, second=0, microsecond=0)
    for _ in range(8):
        if session.head(url_for(cycle, last_hour) + ".idx", timeout=30).status_code == 200:
            return cycle
        cycle -= timedelta(hours=every)
    raise RuntimeError("No complete cycle found in the last two days")


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


def shade(ax, x, y, values, scale, **kw):
    cmap, norm = scale
    return ax.contourf(x, y, values, levels=norm.boundaries, cmap=cmap, norm=norm, zorder=2, **kw)


def contours(ax, x, y, values, levels, color="#ffffff", width=0.9, dashed=False):
    cs = ax.contour(x, y, values, levels=levels, colors=color, linewidths=width,
                    linestyles="--" if dashed else "-", zorder=4)
    ax.clabel(cs, fmt="%d", fontsize=8, inline=True, inline_spacing=2)


def barbs(ax, x, y, u, v):
    """GFS winds are earth-relative and Mercator keeps angles, so barbs can be drawn straight on."""
    s = (slice(None, None, 10), slice(None, None, 10))
    ax.barbs(x[s], y[s], u.values[s] * 1.94384, v.values[s] * 1.94384,  # m/s -> kt
             length=5, linewidth=0.55, color="#dfe6ff", zorder=5)


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
    }
    for lev in {700, 200} | {lev for pair in TURB_LAYERS for lev in pair}:
        for var, key in (("HGT", "z"), ("UGRD", "u"), ("VGRD", "v")):
            wanted[f"{key}{lev}"] = f"{var}:{lev} mb:"
    for lev in ICING_LEVELS:
        for var, key in (("TMP", "t"), ("RH", "rh"), ("CLMR", "clw")):
            wanted[f"{key}{lev}"] = f"{var}:{lev} mb:"
    return wanted


def gfs_precip(f, path):
    x, y = lonlat(f["mslp"])
    fig, ax = new_overlay()
    shade(ax, x, y, f["apcp"].values / 25.4, PRECIP)   # mm -> in
    contours(ax, x, y, gaussian_filter(f["mslp"].values / 100, sigma=1.5), np.arange(940, 1061, 4))
    save(fig, path)


def gfs_vort700(f, path):
    u, v, hgt = f["u700"], f["v700"], f["z700"]
    x, y = lonlat(hgt)
    vort = mpcalc.vorticity(u.metpy.quantify(), v.metpy.quantify())
    vort = gaussian_filter(vort.metpy.dequantify().values * 1e5, sigma=2)
    fig, ax = new_overlay()
    shade(ax, x, y, vort, VORT, extend="both")
    contours(ax, x, y, gaussian_filter(hgt.values / 10, sigma=1.5), np.arange(240, 330, 3))
    barbs(ax, x, y, u, v)
    save(fig, path)


def gfs_wind200(f, path):
    u, v, hgt = f["u200"], f["v200"], f["z200"]
    x, y = lonlat(hgt)
    fig, ax = new_overlay()
    shade(ax, x, y, np.hypot(u.values, v.values) * 1.94384, JET, alpha=0.9, extend="max")
    contours(ax, x, y, gaussian_filter(hgt.values / 10, sigma=1.5), np.arange(1080, 1260, 12))
    barbs(ax, x, y, u, v)
    save(fig, path)


def gfs_clouds(f, path):
    x, y = lonlat(f["tcdc"])
    ceil = f["ceil"].values
    # Ceiling is a height above sea level; 20,000 m means no ceiling.
    ceil_ft = np.where(ceil < 15000, (ceil - f["zsfc"].values) * 3.28084, np.nan)
    fig, ax = new_overlay()
    shade(ax, x, y, f["tcdc"].values, CLOUD)
    ax.contourf(x, y, ceil_ft, levels=[0] + [c[0] for c in CEILING_CATS],
                colors=[c[2] for c in CEILING_CATS], alpha=0.85, zorder=2.5)
    save(fig, path)


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


def gfs_turb(f, path):
    x, y = lonlat(f["u250"])
    jet = gaussian_filter(np.hypot(f["u250"].values, f["v250"].values) * 1.94384, sigma=1)
    fig, ax = new_overlay()
    shade(ax, x, y, ellrod_ti2(f), TURB, extend="max")
    contours(ax, x, y, jet, [70, 90, 110, 130, 150, 170], width=0.8)
    save(fig, path)


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


def gfs_icing(f, path):
    x, y = lonlat(f["psfc"])
    fig, ax = new_overlay()
    shade(ax, x, y, icing_potential(f), ICE)
    contours(ax, x, y, gaussian_filter(f["frzl"].values * 3.28084 / 1000, sigma=1.5), np.arange(2, 17, 2),
             color="#c9d2f0", width=0.7, dashed=True)
    save(fig, path)


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
    }


def simple(field, scale, convert=lambda v: v):
    def draw(f, path):
        x, y = lonlat(f[field])
        fig, ax = new_overlay()
        shade(ax, x, y, convert(f[field].values), scale, extend="max" if scale is not TEMP else "both")
        save(fig, path)
    return draw


K_TO_F = lambda v: (v - 273.15) * 9 / 5 + 32
MS_TO_MPH = lambda v: v * 2.23694


# ---------------------------------------------------------------- HRRR

HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"


def hrrr_url(cycle, fhr):
    return f"{HRRR_BUCKET}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf{fhr:02d}.grib2"


def hrrr_fields(fhr):
    return {
        "refc": f"REFC:entire atmosphere:{fhr} hour fcst$",
        "t2m": f"TMP:2 m above ground:{fhr} hour fcst$",
        "gust": f"GUST:surface:{fhr} hour fcst$",
        "apcp": f"APCP:surface:{fhr - 1}-{fhr} hour acc fcst$",
    }


# ---------------------------------------------------------------- models

MODELS = {
    "gfs": {
        "name": "GFS", "long": "GFS 0.25°", "source": "NOAA Global Forecast System via AWS Open Data",
        "url": gfs_url, "hours": list(range(6, 169, 6)), "fields": gfs_fields, "decode": {"subset": GFS_SUBSET},
        "products": [
            ("precip_mslp", "Precip & pressure", gfs_precip,
             legend("6-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0], lines="White lines: sea-level pressure, every 4 hPa")),
            ("vort700", "700 mb vorticity", gfs_vort700,
             legend("Relative vorticity (10⁻⁵ s⁻¹)", VORT_LEVELS, VORT[0], lines="White lines: 700 mb heights (dam) · barbs: wind (kt)")),
            ("wind200", "200 mb jet stream", gfs_wind200,
             legend("Wind speed (kt)", JET_LEVELS, JET[0], lines="White lines: 200 mb heights (dam) · barbs: wind (kt)")),
            ("clouds", "Clouds & ceilings", gfs_clouds,
             legend("Total cloud cover (%)", CLOUD_LEVELS, CLOUD[0], extra=[[c[2], c[1]] for c in CEILING_CATS]),
             "Ceiling is the model's estimate of the lowest broken or overcast cloud base, in feet above ground."),
            ("turb", "Turbulence", gfs_turb,
             legend("Ellrod TI2 (10⁻⁷ s⁻²) · light → severe", TURB_LEVELS, TURB[0], lines="White lines: 250 mb isotachs (kt)"),
             "Clear-air turbulence potential from the Ellrod index (TI2), the highest value in the 400–200 mb "
             "layer (about FL240–FL390). " + ESTIMATE_NOTE),
            ("icing", "Icing", gfs_icing,
             legend("Icing potential · low → high", ICE_LEVELS, ICE[0], lines="Dashed lines: freezing level (thousands of ft)"),
             "Highest icing potential between 900 and 400 mb, from temperature (0 to −22 °C) and model cloud "
             "liquid water or humidity. " + ESTIMATE_NOTE),
        ],
    },
    "nbm": {
        "name": "NBM", "long": "National Blend of Models", "source": "NOAA National Blend of Models via AWS Open Data",
        "url": nbm_url, "hours": list(range(6, 169, 6)), "fields": nbm_fields, "decode": {"step": 2},
        "products": [
            ("temp", "Temperature", simple("t2m", TEMP, K_TO_F), legend("2 m temperature (°F)", TEMP_LEVELS, TEMP[0])),
            ("precip", "6-hour precip", simple("apcp", PRECIP, lambda v: v / 25.4), legend("6-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0])),
            ("snow", "6-hour snow", simple("snow", SNOW, lambda v: v * 39.3701), legend("6-hour snowfall (in)", SNOW_LEVELS, SNOW[0])),
            ("gust", "Wind gusts", simple("gust", GUST, MS_TO_MPH), legend("Wind gust (mph)", GUST_LEVELS, GUST[0])),
            ("sky", "Sky cover", simple("sky", CLOUD), legend("Sky cover (%)", CLOUD_LEVELS, CLOUD[0])),
        ],
        "note": "The National Blend of Models combines many models and is the starting point for National Weather Service forecasts.",
    },
    "hrrr": {
        "name": "HRRR", "long": "HRRR 3 km", "source": "NOAA High-Resolution Rapid Refresh via AWS Open Data",
        "url": hrrr_url, "hours": list(range(1, 49)), "fields": hrrr_fields, "decode": {"step": 2},
        "products": [
            ("refc", "Simulated radar", simple("refc", REFL), legend("Composite reflectivity (dBZ)", REFL_LEVELS, REFL[0])),
            ("temp", "Temperature", simple("t2m", TEMP, K_TO_F), legend("2 m temperature (°F)", TEMP_LEVELS, TEMP[0])),
            ("precip", "1-hour precip", simple("apcp", PRECIP, lambda v: v / 25.4), legend("1-hour precipitation (in)", PRECIP_LEVELS, PRECIP[0])),
            ("gust", "Wind gusts", simple("gust", GUST, MS_TO_MPH), legend("Wind gust (mph)", GUST_LEVELS, GUST[0])),
        ],
        "note": "The HRRR is NOAA's hourly-updated 3 km model. Its 00, 06, 12 and 18Z runs go out 48 hours.",
    },
}


def run(model_id):
    m = MODELS[model_id]
    hours = m["hours"][: int(os.environ["FW_HOURS"])] if os.environ.get("FW_HOURS") else m["hours"]
    img = OUT / "img" / model_id
    img.mkdir(parents=True, exist_ok=True)
    cycle = latest_cycle(m["url"], m["hours"][-1])
    print(f"{m['long']}: cycle {cycle:%Y-%m-%d %HZ}")
    done = []
    for fhr in hours:
        try:
            fields = get_fields(m["url"](cycle, fhr), m["fields"](fhr), **m["decode"])
        except Exception as e:   # one bad hour shouldn't sink the run
            print(f"  F{fhr:03d} skipped: {e}")
            continue
        for pid, _, draw, *_ in m["products"]:
            draw(fields, img / f"{pid}_f{fhr:03d}.png")
        done.append(fhr)
        print(f"  F{fhr:03d} done")
    if not done:
        raise SystemExit(f"{model_id}: no forecast hours drawn")
    info = {
        "id": model_id, "name": m["name"], "long": m["long"], "source": m["source"], "note": m.get("note"),
        "cycle": cycle.strftime("%Y-%m-%dT%HZ"), "init": cycle.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hours": done,
        "products": [{"id": pid, "name": name, "legend": leg, **({"note": rest[0]} if rest else {})}
                     for pid, name, _, leg, *rest in m["products"]],
    }
    (OUT / "models").mkdir(exist_ok=True)
    (OUT / "models" / f"{model_id}.json").write_text(json.dumps(info, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODELS:
        raise SystemExit(f"usage: make_plots.py {{{','.join(MODELS)}}}")
    run(sys.argv[1])
