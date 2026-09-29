"""Build the seed ASIN list that the reference marketplace is collected for.

``asins.source`` in config.yaml picks where the ASINs come from:

  mcauley            child ASINs reviewed in ``review_window`` (run `amsense mcauley` first)
  file               your own list: .txt (one per line), .csv or .parquet with an ``asin`` column
  keepa_finder       a Keepa Product Finder query, as JSON (keepa.com > Product Finder >
                     "Show API query"), run against the reference marketplace
  keepa_bestsellers  best-seller lists for one or more Amazon category node ids

The list is written once to ``paths.seed``; ``collect`` and ``universe`` read
it from there, so the rest of the pipeline does not care where it came from.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from amsense import marketplaces as mkt
from amsense.config import Settings
from amsense.keepa_client import KeepaClient

log = logging.getLogger(__name__)


FINDER_MAX_PER_PAGE = 10_000


def _dedup(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in values:
        a = (a or "").strip()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def read_asin_file(path: Path, column: str = "asin") -> list[str]:
    """ASINs from a .txt (one per line), .csv or .parquet file, in file order."""
    if not path.exists():
        raise SystemExit(f"ASIN file not found: {path}")
    if path.suffix.lower() in (".parquet", ".csv"):
        reader = "read_parquet" if path.suffix.lower() == ".parquet" else "read_csv_auto"
        rows = duckdb.sql(f"SELECT \"{column}\" FROM {reader}('{path.as_posix()}')").fetchall()
        return _dedup(r[0] for r in rows)
    return _dedup(path.read_text(encoding="utf-8").splitlines())


def _from_finder(settings: Settings, client: KeepaClient) -> list[str]:
    path = Path(settings.asin_source["finder_selection"] or "")
    if not path.is_file():
        raise SystemExit(f"asins.finder_selection must point to a Product Finder JSON file (got {path})")
    selection = json.loads(path.read_text(encoding="utf-8"))
    cap = settings.asin_source["max_asins"]
    per_page = min(int(selection.get("perPage", FINDER_MAX_PER_PAGE)), FINDER_MAX_PER_PAGE)
    out: list[str] = []
    page = int(selection.get("page", 0))
    while True:
        data = client.query({**selection, "perPage": per_page, "page": page})
        batch = data.get("asinList") or []
        out.extend(batch)
        log.info("finder page %d: %d ASINs (total matches %s)", page, len(batch), data.get("totalResults"))
        if len(batch) < per_page or (cap and len(out) >= cap):
            return out
        page += 1


def _from_bestsellers(settings: Settings, client: KeepaClient) -> list[str]:
    cats = settings.asin_source["bestseller_categories"]
    if not cats:
        raise SystemExit("asins.bestseller_categories is empty; list one or more Amazon category node ids")
    out: list[str] = []
    for cat in cats:
        batch = client.bestsellers(cat)
        log.info("best sellers of category %s: %d ASINs", cat, len(batch))
        out.extend(batch)
    return out


def build_seed(settings: Settings, force: bool = False) -> list[str]:
    """Write the seed list to ``paths.seed`` and return it."""
    out = settings.seed_path
    if out.exists() and not force:
        raise SystemExit(f"{out} already exists; pass --force to rebuild it")
    src = settings.asin_source["source"]
    if src == "mcauley":
        if not settings.reviews_path.exists():
            raise SystemExit(f"McAuley reviews not found at {settings.reviews_path}. Run `python -m amsense mcauley`.")
        asins = read_asin_file(settings.reviews_path)
    elif src == "file":
        asins = read_asin_file(Path(settings.asin_source["file"] or ""), settings.asin_source["column"])
    else:
        ref = mkt.get(settings.reference_marketplace)
        client = KeepaClient(settings.keepa_api_key, domain=ref.domain)
        asins = _from_finder(settings, client) if src == "keepa_finder" else _from_bestsellers(settings, client)
        log.info("discovery used %d tokens", client.total_tokens_consumed)

    asins = _dedup(asins)
    cap = settings.asin_source["max_asins"]
    if cap and len(asins) > cap:
        log.info("keeping the first %d of %d ASINs (asins.max_asins)", cap, len(asins))
        asins = asins[:cap]
    if not asins:
        raise SystemExit(f"asins.source={src} produced no ASINs")
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"asin": asins}), out)
    log.info("wrote %s: %s ASINs from source=%s", out, f"{len(asins):,}", src)
    return asins


def load_seed(settings: Settings) -> list[str]:
    """The seed list, building it first if it does not exist yet."""
    if not settings.seed_path.exists():
        return build_seed(settings)
    return read_asin_file(settings.seed_path)
