"""Build the shared ASIN universe from the reference marketplace.

Universe = McAuley child ASINs that have a current price (> 0) in the reference
marketplace's Keepa snapshot, minus those above the ``price_quantile_cut``
quantile of that price. Every other marketplace is queried with exactly this
list, so an ASIN missing from, say, the Canadian panel means Keepa has no data
for it in Canada, not that a country-specific filter removed it.

Writes the universe parquet (``asin``, ``current_price``) plus a small CSV of
the funnel counts and price percentiles next to it.
"""

from __future__ import annotations

import logging

import duckdb

from amsense.config import Settings
from amsense.storage import valid_parts

log = logging.getLogger(__name__)


def build(settings: Settings, force: bool = False) -> int:
    if settings.universe_path.exists() and not force:
        raise SystemExit(
            f"{settings.universe_path} already exists. Other marketplaces were (or will be) collected "
            "against it; rebuilding changes the ASIN set. Pass --force if that is what you want."
        )
    ref = settings.reference_marketplace
    q = settings.price_quantile_cut
    snap = valid_parts(settings.keepa_dir(ref) / "snapshot")
    cov = valid_parts(settings.keepa_dir(ref) / "coverage")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW snapshot AS SELECT * FROM read_parquet({snap!r})")
    con.execute(f"CREATE VIEW coverage AS SELECT * FROM read_parquet({cov!r})")
    con.execute(f"CREATE VIEW reviews AS SELECT asin FROM read_parquet('{settings.reviews_path.as_posix()}')")

    con.execute("""
        CREATE TEMP TABLE pool AS
        SELECT asin, any_value(price) AS current_price FROM snapshot
        WHERE asin IN (SELECT asin FROM reviews) AND price > 0
        GROUP BY asin
    """)
    cutoff = con.execute(f"SELECT quantile_cont(current_price, {q}) FROM pool").fetchone()[0]
    funnel = con.execute(f"""
        SELECT
          (SELECT count(DISTINCT asin) FROM reviews)                                  AS mcauley_child_asins,
          (SELECT count(DISTINCT asin) FROM coverage WHERE found_in_keepa)            AS found_in_keepa,
          (SELECT count(*) FROM pool)                                                 AS with_current_price,
          (SELECT count(*) FROM pool WHERE current_price > {cutoff})                  AS dropped_above_cut,
          (SELECT count(*) FROM pool WHERE current_price <= {cutoff})                 AS universe,
          {cutoff}                                                                    AS price_cutoff,
          {q}                                                                         AS quantile
    """).fetchdf()
    log.info("universe funnel (reference=%s):\n%s", ref, funnel.T.to_string(header=False))

    out = settings.universe_path
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT asin, current_price FROM pool WHERE current_price <= {cutoff} ORDER BY asin)
        TO '{out.as_posix()}' (FORMAT PARQUET)
    """)
    funnel.to_csv(out.with_suffix(".funnel.csv"), index=False)
    n = int(funnel["universe"].iloc[0])
    log.info("wrote %s (%s ASINs)", out, f"{n:,}")
    return n
