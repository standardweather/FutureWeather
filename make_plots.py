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
from concurrent.futures import ThreadPoolExecutor
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
from scipy.ndimage import gaussian_filter

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
FORECAST_HOURS = list(range(6, 169, 6))  # F006 through F168 (7 days), every 6 hours

# CONUS subset of the global grid (GFS longitudes run 0-360)
LAT_SLICE = slice(60, 15)
LON_SLICE = slice(225, 300)
MAP_EXTENT = [-121, -73, 22, 50]
PROJ = ccrs.LambertConformal(central_longitude=-96, standard_parallels=(33, 45))
DATA_CRS = ccrs.PlateCarree()

OUT = Path(__file__).parent / "site"
IMG = OUT / "img"

ESTIMATE_NOTE = ("Diagnostic estimate calculated from GFS output, not an official aviation "
                 "forecast. See aviationweather.gov for operational guidance.")

PRODUCTS = [
    {"id": "precip_mslp", "name": "6-hr Precip & MSLP"},
    {"id": "vort700", "name": "700 mb Vorticity"},
    {"id": "wind200", "name": "200 mb Wind Speed"},
    {"id": "clouds", "name": "Cloud Cover & Ceiling",
     "note": "Ceiling is the model's estimate of the lowest broken or overcast cloud base, "
             "in feet above ground."},
    {"id": "turb", "name": "Turbulence",
     "note": "Clear-air turbulence potential from the Ellrod index (TI2), the highest value "
             "in the 400–200 mb layer (about FL240–FL390). " + ESTIMATE_NOTE},
    {"id": "icing", "name": "Icing Potential",
     "note": "Highest icing potential between 900 and 400 mb, from temperature (0 to −22 °C) "
             "and model cloud liquid water or humidity. " + ESTIMATE_NOTE},
]

TURB_LAYERS = [(400, 300), (300, 250), (250, 200)]   # (lower, upper) mb
ICING_LEVELS = [900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400]

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


def byte_range(index, match, fhr):
    """Byte range of the first GRIB message whose index line contains `match`."""
    for i, (start, desc) in enumerate(index):
        if match in desc:
            end = index[i + 1][0] - 1 if i + 1 < len(index) else ""
            return start, end
    raise KeyError(f"{match!r} not in F{fhr:03d} index")


def decode(content):
    """GRIB message bytes -> CONUS DataArray."""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
        f.write(content)
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


def get_fields(cycle, fhr, wanted):
    """Download {name: index match} fields for one forecast hour.

    Downloads run in parallel; decoding stays on one thread because
    ecCodes isn't guaranteed to be thread-safe.
    """
    index = read_index(cycle, fhr)
    url = file_url(cycle, fhr)

    def fetch(match):
        start, end = byte_range(index, match, fhr)
        resp = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
        resp.raise_for_status()
        return resp.content

    with ThreadPoolExecutor(max_workers=12) as pool:
        blobs = dict(zip(wanted, pool.map(fetch, wanted.values())))
    return {name: decode(blob) for name, blob in blobs.items()}


def fields_for(fhr):
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


# Shared hazard scale for turbulence and icing: muted teal for low values,
# then the site's cyan, amber (--err), orange and red (--tornado) for the worst.
HAZARD_COLORS = ["#2b6f86", "#4ad1ff", "#ffd166", "#ff9f43", "#ff453a"]


def hazard_norm(levels):
    cmap = mcolors.ListedColormap(HAZARD_COLORS[:len(levels) - 1])
    cmap.set_over("#ffd0e0")
    return cmap, mcolors.BoundaryNorm(levels, cmap.N)


CLOUD_LEVELS = [10, 30, 50, 70, 90, 100]
CLOUD_CMAP = mcolors.ListedColormap(["#2a3358", "#46507a", "#6f789e", "#a3aac6", "#dfe3f1"])
CLOUD_NORM = mcolors.BoundaryNorm(CLOUD_LEVELS, CLOUD_CMAP.N)

# Standard flight-category colors, which line up with the site palette.
CEILING_CATS = [  # (upper bound ft AGL, label, color)
    (500, "LIFR  < 500 ft", "#e04fd0"),
    (1000, "IFR  500–999 ft", "#ff453a"),
    (3000, "MVFR  1,000–3,000 ft", "#4ad1ff"),
]


def plot_clouds(cycle, fhr, f):
    lon, lat = lonlat(f["tcdc"])
    ceil = f["ceil"].values
    # Ceiling is a height above sea level; 20,000 m means no ceiling.
    ceil_ft = np.where(ceil < 15000, (ceil - f["zsfc"].values) * 3.28084, np.nan)

    fig, ax = new_map()
    cf = ax.contourf(lon, lat, f["tcdc"].values, levels=CLOUD_LEVELS, cmap=CLOUD_CMAP,
                     norm=CLOUD_NORM, transform=DATA_CRS, zorder=2)
    bounds = [0] + [c[0] for c in CEILING_CATS]
    ax.contourf(lon, lat, ceil_ft, levels=bounds,
                colors=[c[2] for c in CEILING_CATS], alpha=0.85,
                transform=DATA_CRS, zorder=2.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c[2], alpha=0.85) for c in CEILING_CATS]
    leg = ax.legend(handles, [c[1] for c in CEILING_CATS], title="Ceiling (AGL)",
                    loc="lower left", fontsize=8, title_fontsize=8, framealpha=0.85,
                    facecolor=BG, edgecolor="#2a3156", labelcolor=TEXT)
    leg.get_title().set_color(TEXT)
    leg.set_zorder(6)
    finish(fig, ax, cf, "Total cloud cover (%)", "Total Cloud Cover (%), Low Ceilings",
           cycle, fhr, IMG / f"clouds_f{fhr:03d}.png")


def with_units(da, unit):
    da = da.copy()
    da.attrs["units"] = unit
    return da.metpy.quantify()


def ellrod_ti2(f):
    """Ellrod-Knapp TI2 (10^-7 s^-2): vertical shear x (deformation + convergence).

    Computed for each layer in TURB_LAYERS; the largest value is kept.
    """
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


# The published Ellrod thresholds (4/8/12) were tuned on coarser grids. At 0.25 deg the
# gradients are sharper and values run about 3x higher, so the scale is shifted up:
# roughly 10% of CONUS gets any shading and ~1% reaches orange or red.
TURB_LEVELS = [12, 20, 30, 40, 50, 60]


def plot_turb(cycle, fhr, f):
    lon, lat = lonlat(f["u250"])
    ti2 = ellrod_ti2(f)
    jet = gaussian_filter(np.hypot(f["u250"].values, f["v250"].values) * 1.94384, sigma=1)

    fig, ax = new_map()
    cmap, norm = hazard_norm(TURB_LEVELS)
    cf = ax.contourf(lon, lat, ti2, levels=TURB_LEVELS, cmap=cmap, norm=norm,
                     extend="max", transform=DATA_CRS, zorder=2)
    cs = ax.contour(lon, lat, jet, levels=[70, 90, 110, 130, 150, 170], colors=CONTOUR,
                    linewidths=0.8, transform=DATA_CRS, zorder=4)
    ax.clabel(cs, fmt="%d", fontsize=7, inline=True, inline_spacing=2)
    finish(fig, ax, cf, "Ellrod TI2 (10⁻⁷ s⁻²)   light → moderate → severe",
           "Turbulence FL240–390, 250 mb Isotachs (kt)",
           cycle, fhr, IMG / f"turb_f{fhr:03d}.png")


def icing_potential(f):
    """Max over ICING_LEVELS of temperature interest x moisture interest (0-1)."""
    psfc = f["psfc"].values / 100  # Pa -> hPa
    best = np.zeros_like(psfc)
    for lev in ICING_LEVELS:
        t_c = f[f"t{lev}"].values - 273.15
        # Supercooled liquid is most likely from about -4 to -14 C.
        t_score = np.interp(t_c, [-22, -14, -4, 0], [0, 1, 1, 0], left=0, right=0)
        clw = f[f"clw{lev}"].values * 1000  # kg/kg -> g/kg
        clw_score = np.interp(clw, [0.01, 0.2], [0, 1])
        # Humidity alone (no model cloud water) counts for less.
        rh_score = 0.6 * np.interp(f[f"rh{lev}"].values, [70, 95], [0, 1])
        pot = t_score * np.maximum(clw_score, rh_score)
        pot[psfc < lev] = 0  # level is below ground
        best = np.maximum(best, pot)
    return gaussian_filter(best, sigma=1)


# Humidity-only areas top out at 0.6; amber and above need model cloud liquid water.
ICING_LEVELS_SHADE = [0.2, 0.4, 0.6, 0.7, 0.85, 1.0]


def plot_icing(cycle, fhr, f):
    lon, lat = lonlat(f["psfc"])
    pot = icing_potential(f)
    frz_kft = gaussian_filter(f["frzl"].values * 3.28084 / 1000, sigma=1.5)

    fig, ax = new_map()
    cmap, norm = hazard_norm(ICING_LEVELS_SHADE)
    cf = ax.contourf(lon, lat, pot, levels=ICING_LEVELS_SHADE, cmap=cmap, norm=norm,
                     transform=DATA_CRS, zorder=2)
    cs = ax.contour(lon, lat, frz_kft, levels=np.arange(2, 17, 2), colors=TEXT2,
                    linewidths=0.7, linestyles="--", transform=DATA_CRS, zorder=4)
    ax.clabel(cs, fmt="%d", fontsize=7, inline=True, inline_spacing=2)
    finish(fig, ax, cf, "Icing potential   low → high",
           "Icing Potential 900–400 mb, Freezing Level (kft)",
           cycle, fhr, IMG / f"icing_f{fhr:03d}.png")


# ---------------------------------------------------------------- main

def main():
    IMG.mkdir(parents=True, exist_ok=True)
    cycle = latest_complete_cycle()
    print(f"Using GFS cycle {cycle:%Y-%m-%d %HZ}")

    for fhr in FORECAST_HOURS:
        fields = get_fields(cycle, fhr, fields_for(fhr))
        plot_precip_mslp(cycle, fhr, fields)
        plot_vort700(cycle, fhr, fields)
        plot_wind200(cycle, fhr, fields)
        plot_clouds(cycle, fhr, fields)
        plot_turb(cycle, fhr, fields)
        plot_icing(cycle, fhr, fields)
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
