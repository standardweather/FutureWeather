"""Download the latest GFS run and draw CONUS maps for the web page.

Data comes from NOAA's GFS bucket on AWS. Only the needed fields are
downloaded, using the byte offsets listed in each file's .idx index.

Output:
  site/img/<product>_f<hhh>.png   one image per product per forecast hour
  site/data.json                  run info the web page reads
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import metpy.calc as mpcalc
import numpy as np
import requests
import xarray as xr
from metpy.units import units
from scipy.ndimage import gaussian_filter

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
FORECAST_HOURS = list(range(6, 73, 6))  # F006 through F072, every 6 hours

# CONUS subset of the global grid (GFS longitudes run 0-360)
LAT_SLICE = slice(60, 15)
LON_SLICE = slice(225, 300)
MAP_EXTENT = [-121, -73, 22, 50]
PROJ = ccrs.LambertConformal(central_longitude=-96, standard_parallels=(33, 45))
DATA_CRS = ccrs.PlateCarree()

OUT = Path(__file__).parent / "site"
IMG = OUT / "img"

PRODUCTS = [
    {"id": "precip_mslp", "name": "6-hr Precip & MSLP"},
    {"id": "vort700", "name": "700 mb Vorticity"},
    {"id": "wind200", "name": "200 mb Wind Speed"},
]

session = requests.Session()


# ---------------------------------------------------------------- data access

def file_url(cycle, fhr):
    return (f"{BUCKET}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
            f"gfs.t{cycle:%H}z.pgrb2.0p25.f{fhr:03d}")


def latest_complete_cycle():
    """Newest cycle whose last forecast hour has been posted."""
    now = datetime.now(timezone.utc)
    cycle = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    for _ in range(6):
        url = file_url(cycle, FORECAST_HOURS[-1]) + ".idx"
        if session.head(url, timeout=30).status_code == 200:
            return cycle
        cycle -= timedelta(hours=6)
    raise RuntimeError("No complete GFS cycle found in the last 36 hours")


def read_index(cycle, fhr):
    """Return [(offset, description)] for every message in the file."""
    text = session.get(file_url(cycle, fhr) + ".idx", timeout=60).text
    entries = []
    for line in text.strip().splitlines():
        parts = line.split(":")
        entries.append((int(parts[1]), ":".join(parts[3:])))
    return entries


def get_field(cycle, fhr, index, match):
    """Download one GRIB message whose index line contains `match`."""
    for i, (start, desc) in enumerate(index):
        if match in desc:
            end = index[i + 1][0] - 1 if i + 1 < len(index) else ""
            break
    else:
        raise KeyError(f"{match!r} not in F{fhr:03d} index")

    resp = session.get(file_url(cycle, fhr),
                       headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    resp.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(resp.content)
        path = f.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        da = ds[list(ds.data_vars)[0]]
        da = da.sel(latitude=LAT_SLICE, longitude=LON_SLICE).load()
        # Extra time coords make MetPy warn; the run/valid times are tracked separately.
        da = da.drop_vars(["time", "step", "valid_time"], errors="ignore")
    finally:
        os.remove(path)
    return da


# ---------------------------------------------------------------- plotting

# Dark theme matching standardweather.github.io (css/site.css).
BG = "#0d1020"        # page panel background
LAND = "#1a2140"
WATER = "#0f1430"
LINES = "#8a93b8"     # state lines, coastlines
CONTOUR = "#ffffff"
BARBS = "#c9d2f0"
TEXT = "#ffffff"
TEXT2 = "#aab2cc"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "text.color": TEXT,
    "axes.labelcolor": TEXT2,
    "xtick.color": TEXT2,
    "ytick.color": TEXT2,
    "savefig.facecolor": BG,
})


def new_map():
    fig = plt.figure(figsize=(11, 7.6), facecolor=BG)
    ax = fig.add_axes([0.01, 0.11, 0.98, 0.84], projection=PROJ)
    ax.set_extent(MAP_EXTENT, crs=DATA_CRS)
    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=WATER, zorder=0)
    ax.add_feature(cfeature.LAKES.with_scale("50m"), facecolor=WATER, zorder=0)
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=LAND, zorder=0)
    ax.add_feature(cfeature.STATES.with_scale("50m"), linewidth=0.4, edgecolor=LINES, zorder=3)
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.7, edgecolor=LINES, zorder=3)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.7, edgecolor=LINES, zorder=3)
    ax.spines["geo"].set_edgecolor("#2a3156")
    return fig, ax


def finish(fig, ax, mappable, label, title, cycle, fhr, path):
    valid = cycle + timedelta(hours=fhr)
    ax.set_title(f"GFS 0.25°  {title}", loc="left", fontsize=11, fontweight="bold")
    ax.set_title(f"Init {cycle:%Y-%m-%d %HZ}   Valid {valid:%a %Y-%m-%d %HZ} (F{fhr:03d})",
                 loc="right", fontsize=10, color=TEXT2)
    cax = fig.add_axes([0.15, 0.07, 0.70, 0.022], facecolor=BG)
    cb = fig.colorbar(mappable, cax=cax, orientation="horizontal")
    if isinstance(mappable.norm, mcolors.BoundaryNorm):
        cb.set_ticks(mappable.norm.boundaries)
        cb.set_ticklabels([f"{b:g}" for b in mappable.norm.boundaries])
    cb.set_label(label, fontsize=9)
    cb.ax.tick_params(labelsize=8)
    cb.outline.set_edgecolor("#2a3156")
    fig.savefig(path, dpi=100)
    plt.close(fig)


def heights(ax, lon, lat, values, levels):
    cs = ax.contour(lon, lat, values, levels=levels, colors=CONTOUR,
                    linewidths=0.9, transform=DATA_CRS, zorder=4)
    ax.clabel(cs, fmt="%d", fontsize=7, inline=True, inline_spacing=2)


def barbs(ax, lon, lat, u, v):
    skip = (slice(None, None, 10), slice(None, None, 10))
    ax.barbs(lon[skip], lat[skip],
             u.values[skip] * 1.94384, v.values[skip] * 1.94384,  # m/s -> kt
             length=5, linewidth=0.5, color=BARBS, transform=DATA_CRS, zorder=5)


def lonlat(da):
    return np.meshgrid(da.longitude.values - 360, da.latitude.values)


PRECIP_LEVELS = [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6]
PRECIP_COLORS = ["#c8f0c0", "#8fd88a", "#41ab5d", "#1f7f3b", "#0a5a2a",
                 "#f6e33c", "#f2a33a", "#e8532b", "#c41d1d", "#a0157a", "#d38ae8"]


def plot_precip_mslp(cycle, fhr, f):
    lon, lat = lonlat(f["mslp"])
    precip = f["apcp"].values / 25.4  # mm -> inches
    mslp = gaussian_filter(f["mslp"].values / 100, sigma=1.5)  # Pa -> hPa

    fig, ax = new_map()
    cmap = mcolors.ListedColormap(PRECIP_COLORS)
    norm = mcolors.BoundaryNorm(PRECIP_LEVELS, cmap.N)
    cf = ax.contourf(lon, lat, precip, levels=PRECIP_LEVELS, cmap=cmap, norm=norm,
                     transform=DATA_CRS, zorder=2)
    heights(ax, lon, lat, mslp, np.arange(940, 1061, 4))
    finish(fig, ax, cf, "6-hr precipitation (in)", "6-hr Precip (in), MSLP (hPa)",
           cycle, fhr, IMG / f"precip_mslp_f{fhr:03d}.png")


# Weak vorticity (|ζ| < 4) is left clear; stronger values get brighter in each direction.
VORT_LEVELS = [-24, -20, -16, -12, -8, -4, 4, 8, 12, 16, 20, 24]
VORT_CMAP = mcolors.ListedColormap(
    ["#9be7ff", "#4ad1ff", "#2f9fd8", "#2a6fb0", "#24497f",   # anticyclonic
     (0, 0, 0, 0),                                             # -4 to 4: unshaded
     "#7a2f3a", "#b8423f", "#e0603a", "#f5923e", "#ffd166"])  # cyclonic
VORT_CMAP.set_under("#d4f5ff")
VORT_CMAP.set_over("#fff1b8")
VORT_NORM = mcolors.BoundaryNorm(VORT_LEVELS, VORT_CMAP.N)


def plot_vort700(cycle, fhr, f):
    u, v, hgt = f["u700"], f["v700"], f["z700"]
    lon, lat = lonlat(hgt)
    vort = mpcalc.vorticity(u.metpy.quantify(), v.metpy.quantify())
    vort = gaussian_filter(vort.metpy.dequantify().values * 1e5, sigma=2)
    hgt_dam = gaussian_filter(hgt.values / 10, sigma=1.5)

    fig, ax = new_map()
    cf = ax.contourf(lon, lat, vort, levels=VORT_LEVELS, cmap=VORT_CMAP, norm=VORT_NORM,
                     extend="both", transform=DATA_CRS, zorder=2)
    heights(ax, lon, lat, hgt_dam, np.arange(240, 330, 3))
    barbs(ax, lon, lat, u, v)
    finish(fig, ax, cf, "Relative vorticity (10⁻⁵ s⁻¹)",
           "700 mb Rel. Vorticity, Heights (dam), Wind (kt)",
           cycle, fhr, IMG / f"vort700_f{fhr:03d}.png")


def plot_wind200(cycle, fhr, f):
    u, v, hgt = f["u200"], f["v200"], f["z200"]
    lon, lat = lonlat(hgt)
    speed = np.hypot(u.values, v.values) * 1.94384  # m/s -> kt
    hgt_dam = gaussian_filter(hgt.values / 10, sigma=1.5)

    fig, ax = new_map()
    # Cyan -> violet -> magenta, echoing the site's accent gradient.
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "jet_stream", ["#1b4f7a", "#4ad1ff", "#8f6bff", "#e04fd0", "#ffd1f5"])
    cf = ax.contourf(lon, lat, speed, levels=np.arange(50, 201, 10), cmap=cmap,
                     extend="max", alpha=0.9, transform=DATA_CRS, zorder=2)
    heights(ax, lon, lat, hgt_dam, np.arange(1080, 1260, 12))
    barbs(ax, lon, lat, u, v)
    finish(fig, ax, cf, "Wind speed (kt)", "200 mb Wind Speed (kt), Heights (dam)",
           cycle, fhr, IMG / f"wind200_f{fhr:03d}.png")


# ---------------------------------------------------------------- main

def main():
    IMG.mkdir(parents=True, exist_ok=True)
    cycle = latest_complete_cycle()
    print(f"Using GFS cycle {cycle:%Y-%m-%d %HZ}")

    for fhr in FORECAST_HOURS:
        index = read_index(cycle, fhr)
        fields = {
            "mslp": get_field(cycle, fhr, index, "PRMSL:mean sea level"),
            "apcp": get_field(cycle, fhr, index, f"APCP:surface:{fhr - 6}-{fhr} hour acc"),
            "z700": get_field(cycle, fhr, index, "HGT:700 mb:"),
            "u700": get_field(cycle, fhr, index, "UGRD:700 mb:"),
            "v700": get_field(cycle, fhr, index, "VGRD:700 mb:"),
            "z200": get_field(cycle, fhr, index, "HGT:200 mb:"),
            "u200": get_field(cycle, fhr, index, "UGRD:200 mb:"),
            "v200": get_field(cycle, fhr, index, "VGRD:200 mb:"),
        }
        plot_precip_mslp(cycle, fhr, fields)
        plot_vort700(cycle, fhr, fields)
        plot_wind200(cycle, fhr, fields)
        print(f"  F{fhr:03d} done")

    info = {
        "model": "GFS 0.25°",
        "cycle": cycle.strftime("%Y-%m-%dT%HZ"),
        "init": cycle.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hours": FORECAST_HOURS,
        "products": PRODUCTS,
    }
    (OUT / "data.json").write_text(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
