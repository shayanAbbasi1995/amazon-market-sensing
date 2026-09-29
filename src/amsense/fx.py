"""Monthly FX rates (USD per unit of each currency) from the Bank of Canada Valet API.

Panels stay in local currency. This table is only for analyses that compare
the same product's price across countries; convert with the rate of the month
the price was observed in, never with a single static rate.

Valet publishes every rate against CAD (``FX{CUR}CAD`` = CAD per 1 unit of
CUR), so USD per unit of CUR = FX{CUR}CAD / FXUSDCAD. Free, no key, one request.
"""

from __future__ import annotations

import logging

import pandas as pd
import requests

from amsense.config import Settings
from amsense.marketplaces import MARKETPLACES

log = logging.getLogger(__name__)

VALET = "https://www.bankofcanada.ca/valet/observations/{series}/json"


def fetch(settings: Settings, start: str = "2011-01-01") -> pd.DataFrame:
    """Download daily rates, average them by month, write ``paths.fx``.

    Returns a long table: month (first day), currency, usd_per_unit.
    """
    currencies = sorted({MARKETPLACES[m].currency for m in settings.marketplaces} | {"USD"})
    series = ["FXUSDCAD"] + [f"FX{c}CAD" for c in currencies if c not in ("USD", "CAD")]
    resp = requests.get(VALET.format(series=",".join(series)), params={"start_date": start}, timeout=60)
    resp.raise_for_status()
    obs = resp.json()["observations"]

    rows = []
    for o in obs:
        cad_per_usd = float(o["FXUSDCAD"]["v"]) if "FXUSDCAD" in o else None
        if not cad_per_usd:
            continue
        for c in currencies:
            if c == "USD":
                usd = 1.0
            elif c == "CAD":
                usd = 1.0 / cad_per_usd
            elif f"FX{c}CAD" in o and o[f"FX{c}CAD"].get("v"):
                usd = float(o[f"FX{c}CAD"]["v"]) / cad_per_usd
            else:
                continue
            rows.append((o["d"], c, usd))

    daily = pd.DataFrame(rows, columns=["date", "currency", "usd_per_unit"])
    daily["month"] = pd.to_datetime(daily["date"]).dt.to_period("M").dt.to_timestamp()
    monthly = daily.groupby(["month", "currency"], as_index=False)["usd_per_unit"].mean()
    missing = set(currencies) - set(monthly["currency"])
    if missing:
        log.warning("Valet returned no rates for %s", sorted(missing))

    settings.fx_path.parent.mkdir(parents=True, exist_ok=True)
    monthly.to_parquet(settings.fx_path, index=False)
    log.info(
        "wrote %s: %d months x %d currencies (%s .. %s)",
        settings.fx_path,
        monthly["month"].nunique(),
        monthly["currency"].nunique(),
        monthly["month"].min().date(),
        monthly["month"].max().date(),
    )
    return monthly
