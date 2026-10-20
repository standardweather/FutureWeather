"""Combine the per-model run files from make_plots.py into site/data.json, which the page reads.

Models whose job failed are simply left out. Standard library only, so the deploy job needn't install
the plotting stack.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

SITE = Path(__file__).parent / "site"
ORDER = ["gfs", "nbm", "hrrr"]
# Must match BOUNDS in make_plots.py: [[south, west], [north, east]].
BOUNDS = [[20.0, -130.0], [54.0, -60.0]]

models = []
for mid in ORDER:
    path = SITE / "models" / f"{mid}.json"
    if path.exists():
        models.append(json.loads(path.read_text()))
    else:
        print(f"{mid}: no run this time")
if not models:
    raise SystemExit("no model runs to publish")
(SITE / "data.json").write_text(json.dumps({
    "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "bounds": BOUNDS,
    "models": models,
}, indent=1))
print(f"data.json: {', '.join(m['id'] + ' ' + m['cycle'] for m in models)}")
