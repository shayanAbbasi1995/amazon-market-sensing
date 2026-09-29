"""Build research panels from a marketplace's raw Keepa ``daily`` table.

Panels (pick any in ``panels.build`` in config.yaml):

  monthly_obs   one row per (asin, month) with at least one observation
  monthly_locf  every month from the ASIN's first observation to today, each
                series carried forward from its last observation (LOCF)
  weekly_obs    / weekly_locf   the same at ISO-week (Monday) resolution

Within a period the last observation wins. Only ASINs in the shared universe
are used, so every marketplace's panel covers the same product set. Cleaning
rules come from :mod:`amsense.cleaning`.

The work is split into ``hash(asin) % N`` batches so the LOCF window functions
never hold the whole panel in memory (a single-pass weekly build of 95M rows
ran out of memory at 16 GB). Each batch writes one part file per panel.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import duckdb

from amsense import cleaning
from amsense import marketplaces as mkt
from amsense.config import Settings
from amsense.storage import valid_parts

log = logging.getLogger(__name__)

PANELS = {"monthly_obs": "month", "monthly_locf": "month", "weekly_obs": "week", "weekly_locf": "week"}
SERIES_MAP = cleaning.SERIES_MAP

# Running window per ASIN. MAX over it gives LOCF and a non-decreasing
# review_count in one step; last_value(IGNORE NULLS) gives plain LOCF.
_WIN = "OVER (PARTITION BY asin ORDER BY period ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}'")
    con.execute(f"SET threads={settings.duckdb_threads}")
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{settings.tmp_dir.as_posix()}'")
    return con


def _pivot_sql() -> str:
    return ",\n    ".join(f"max(value) FILTER (WHERE series = '{s}') AS {c}" for s, c in SERIES_MAP.items())


def _aggregate(
    con: duckdb.DuckDBPyConnection, cadence: str, rules: cleaning.CleaningRules, batch: int, n_batches: int
) -> int:
    """Temp table ``wide``: one row per (asin, period), last value per series,
    plus penny flags (derived before LOCF so each flag travels with its price)."""
    series = ", ".join(f"'{s}'" for s in SERIES_MAP)
    # epoch_ms() yields a UTC timestamp; to_timestamp(ms/1000) would shift
    # Jan-1 UTC observations into Dec-31 under a local timezone.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE wide AS
        WITH d AS (
          SELECT d.asin, d.series, d.timestamp, d.value,
                 date_trunc('{cadence}', epoch_ms(d.timestamp))::DATE AS period
          FROM daily d
          JOIN (SELECT asin FROM universe WHERE hash(asin) % {n_batches} = {batch}) u USING (asin)
          WHERE d.series IN ({series})
            AND {cleaning.daily_where_sql(rules, "d")}
        ),
        last_obs AS (
          SELECT asin, series, period, value FROM d
          QUALIFY row_number() OVER (PARTITION BY asin, series, period ORDER BY timestamp DESC) = 1
        )
        SELECT *, {", ".join(cleaning.penny_flag_exprs(rules))}
        FROM (SELECT asin, period, {_pivot_sql()} FROM last_obs GROUP BY asin, period)
    """)
    return con.execute("SELECT count(*) FROM wide").fetchone()[0]


def _write_obs(con: duckdb.DuckDBPyConnection, rules: cleaning.CleaningRules, out: Path) -> None:
    cols = []
    for c in list(SERIES_MAP.values()) + cleaning.penny_flag_cols():
        if c == "review_count":  # clamp to running max, keep NULLs on gap rows
            cols.append(f"CASE WHEN {c} IS NULL THEN NULL ELSE MAX({c}) {_WIN} END AS {c}")
        else:
            cols.append(c)
    con.execute(f"""
        COPY (SELECT asin, period, {cleaning.identity_cols_sql(rules)}, {", ".join(cols)}
              FROM wide ORDER BY asin, period)
        TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION SNAPPY)
    """)


def _write_locf(con: duckdb.DuckDBPyConnection, cadence: str, rules: cleaning.CleaningRules, out: Path) -> None:
    """Calendar from each ASIN's first observed period to the current period.
    Periods before the first observation are never created, so nothing is
    filled in ahead of the data."""
    step = "INTERVAL 7 DAYS" if cadence == "week" else "INTERVAL 1 MONTH"
    now = f"date_trunc('{cadence}', current_date)::DATE"
    cols = []
    for c in list(SERIES_MAP.values()) + cleaning.penny_flag_cols():
        if c == "review_count":
            cols.append(f"MAX({c}) {_WIN} AS {c}")
        else:
            cols.append(f"last_value({c} IGNORE NULLS) {_WIN} AS {c}")
    con.execute(f"""
        COPY (
          WITH span AS (SELECT asin, min(period) AS p0 FROM wide GROUP BY asin),
          calendar AS (
            SELECT asin, UNNEST(generate_series(p0, {now}, {step}))::DATE AS period FROM span
          ),
          joined AS (SELECT * FROM calendar LEFT JOIN wide USING (asin, period))
          SELECT asin, period, {cleaning.identity_cols_sql(rules)}, {", ".join(cols)}
          FROM joined ORDER BY asin, period
        ) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION SNAPPY)
    """)


def build(settings: Settings, locale: str, panels: list[str] | None = None) -> dict[str, int]:
    """Build the requested panels for one marketplace. Returns rows per panel."""
    m = mkt.get(locale)
    panels = list(panels or settings.panels)
    unknown = set(panels) - set(PANELS)
    if unknown:
        raise SystemExit(f"unknown panel(s) {sorted(unknown)}; choose from {list(PANELS)}")
    rules = cleaning.rules_for(m, settings)
    log.info("%s", rules.summary())
    if not settings.universe_path.exists():
        raise SystemExit(f"universe not found: {settings.universe_path}. Run `python -m amsense universe`.")

    con = connect(settings)
    daily = valid_parts(settings.keepa_dir(locale) / "daily")
    con.execute(f"CREATE VIEW daily AS SELECT * FROM read_parquet({daily!r})")
    con.execute(f"CREATE VIEW universe AS SELECT asin FROM read_parquet('{settings.universe_path.as_posix()}')")

    out_root = settings.panels_dir(locale)
    for p in panels:
        (out_root / p).mkdir(parents=True, exist_ok=True)
        for old in (out_root / p).glob("*.parquet"):
            old.unlink()

    n = settings.panel_batches
    for cadence in ("month", "week"):
        todo = [p for p in panels if PANELS[p] == cadence]
        if not todo:
            continue
        t0 = time.time()
        for b in range(n):
            if _aggregate(con, cadence, rules, b, n) == 0:
                continue
            for p in todo:
                out = out_root / p / f"part_{b:03d}.parquet"
                if p.endswith("_obs"):
                    _write_obs(con, rules, out)
                else:
                    _write_locf(con, cadence, rules, out)
        log.info("[%s] %s panels %s built in %.1f min", m.code, cadence, todo, (time.time() - t0) / 60)

    counts = {}
    for p in panels:
        glob = (out_root / p / "*.parquet").as_posix()
        rows, asins, lo, hi = con.execute(
            f"SELECT count(*), count(DISTINCT asin), min(period), max(period) FROM read_parquet('{glob}')"
        ).fetchone()
        counts[p] = rows
        log.info("[%s] %-13s %12s rows  %8s ASINs  %s .. %s", m.code, p, f"{rows:,}", f"{asins:,}", lo, hi)
    return counts
