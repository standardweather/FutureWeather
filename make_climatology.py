"""Build the 500 mb height climatology used for the Upper air map's normalized anomaly (z500_climate.npz).

Daily-mean 500 mb heights from the NCEP/NCAR Reanalysis 1 (2.5°, NOAA PSL), 1991–2020, over North America.
For each grid point the mean and the variance about it are fit with a few annual harmonics, which gives
a smooth seasonal cycle for any day of the year. Run it once; the result is committed.

    python make_climatology.py
"""

import numpy as np
import xarray as xr

URL = "https://psl.noaa.gov/thredds/dodsC/Datasets/ncep.reanalysis/Dailies/pressure/hgt.{year}.nc"
YEARS = range(1991, 2021)
HARMONICS = 3


def design(day_of_year):
    """Columns 1, cos t, sin t, cos 2t, sin 2t, ... for t the fraction of the year in radians."""
    t = 2 * np.pi * (np.asarray(day_of_year, dtype=float) - 1) / 365.25
    cols = [np.ones_like(t)]
    for k in range(1, HARMONICS + 1):
        cols += [np.cos(k * t), np.sin(k * t)]
    return np.column_stack(cols)


def main():
    chunks = []
    for year in YEARS:
        ds = xr.open_dataset(URL.format(year=year))
        chunks.append(ds.hgt.sel(level=500, lat=slice(70, 5), lon=slice(205, 315)).load())
        print(year)
    z = xr.concat(chunks, "time")
    X = design(z.time.dt.dayofyear.values)
    flat = z.values.reshape(len(z.time), -1)
    mean_coef, *_ = np.linalg.lstsq(X, flat, rcond=None)
    var_coef, *_ = np.linalg.lstsq(X, (flat - X @ mean_coef) ** 2, rcond=None)
    shape = (X.shape[1], z.sizes["lat"], z.sizes["lon"])
    np.savez_compressed("z500_climate.npz", lat=z.lat.values, lon=(z.lon.values + 180) % 360 - 180,
                        mean=mean_coef.reshape(shape).astype("float32"), var=var_coef.reshape(shape).astype("float32"))
    print("wrote z500_climate.npz")


if __name__ == "__main__":
    main()
