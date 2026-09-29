"""README figures, drawn from the built panels, the raw daily and coverage
tables, and the FX table.

Only aggregates come out of DuckDB; no panel is loaded into pandas. Every
figure logs the numbers behind it so they can be checked against the image.

  coverage    share of queried ASINs each marketplace has Keepa history for
  overlap     share of the universe listed in both of two marketplaces
  tracked     ASINs with a recorded price change each month
  reviews     review counts after the McAuley snapshot, relative to Sep 2023
  price_gap   same ASIN, same month: price premium over the reference, in USD
  events      daily count of >=10% price cuts through one year
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter, PercentFormatter

from amsense import cleaning
from amsense import marketplaces as mkt
from amsense.config import Settings
from amsense.storage import valid_parts

log = logging.getLogger(__name__)

# Chart chrome and a CVD-validated categorical order (8 slots). Slots go to
# marketplaces in config order, so a color always means the same store within
# one config. Line charts show at most 8 marketplaces; a 9th hue would not be
# distinguishable, so extra stores are named in the figure note instead.
SURFACE, INK, INK2, MUTED, GRID, BASE = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BAND = "#f0efec"
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
COLORS: dict[str, str] = {}  # filled by Data() for the marketplaces in this run
SEQ = LinearSegmentedColormap.from_list("seq", ["#cde2fb", "#86b6ef", "#2a78d6", "#184f95", "#0d366b"])
MCAULEY_END = pd.Timestamp("2023-09-01")
MIN_QUERIED = 0.95  # a marketplace enters cross-country coverage figures once this share is queried

# Amazon sale events in 2024 (dates of the US event; Prime Day and Prime Big
# Deal Days ran on the same days in the other marketplaces shown).
EVENTS_2024 = [
    ("Big Spring Sale", "2024-03-20", "2024-03-25"),
    ("Prime Day", "2024-07-16", "2024-07-17"),
    ("Prime Big Deal Days", "2024-10-08", "2024-10-09"),
    ("Black Friday week", "2024-11-21", "2024-12-02"),
]

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": ["Segoe UI", "DejaVu Sans"],
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.edgecolor": BASE,
        "axes.linewidth": 1,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 1,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "lines.linewidth": 2,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
        "legend.frameon": False,
    }
)


# ── layout helpers ───────────────────────────────────────────────────────────
def _header(fig, title: str, subtitle: str, locales: list[str] | None = None) -> None:
    """Title, subtitle and (for multi-series charts) a legend row under them."""
    fig.text(0.02, 0.965, title, fontsize=14, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.905, subtitle, fontsize=10, color=INK2, va="top")
    if locales:
        handles = [plt.Line2D([], [], color=COLORS[m], lw=2) for m in locales]
        fig.legend(
            handles,
            [mkt.get(m).country for m in locales],
            loc="upper left",
            bbox_to_anchor=(0.012, 0.86),
            ncols=len(locales),
            fontsize=9,
            handlelength=1.4,
            columnspacing=1.4,
        )


def _note(fig, text: str) -> None:
    fig.text(0.02, 0.02, text, fontsize=8, color=MUTED, va="bottom")


def _save(fig, settings: Settings, name: str) -> None:
    out = settings.figures_dir / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200)
    plt.close(fig)
    log.info("wrote %s", out)


def _end_labels(ax, series: dict[str, pd.Series], fmt: Callable[[float], str], min_gap: float) -> None:
    """Label each line at its right end. Labels that would overlap are pushed
    apart vertically and tied back to their line end with a hairline leader."""
    ends = sorted((s.dropna().iloc[-1], s.dropna().index[-1], k) for k, s in series.items() if s.notna().any())
    x_lab = max(x for _, x, _ in ends) + pd.Timedelta(days=75)  # one label column for all lines
    prev = None
    for y, x, k in ends:
        y_lab = y if prev is None else max(y, prev + min_gap)
        prev = y_lab
        ax.plot([x], [y], "o", ms=6, color=COLORS[k], mec=SURFACE, mew=2, zorder=5, clip_on=False)
        moved = abs(y_lab - y) > 1e-12 or x_lab - x > pd.Timedelta(days=80)
        ax.annotate(
            f"{k.upper()}  {fmt(y)}",
            xy=(x, y),
            xytext=(x_lab, y_lab),
            va="center",
            fontsize=9,
            color=INK,
            annotation_clip=False,
            arrowprops={"arrowstyle": "-", "color": BASE, "lw": 0.8, "shrinkA": 0, "shrinkB": 4} if moved else None,
        )


def _mcauley_line(ax) -> None:
    ax.axvline(MCAULEY_END, color=INK2, lw=1, zorder=1)
    ax.text(
        MCAULEY_END,
        0.98,
        " McAuley data ends",
        transform=ax.get_xaxis_transform(),
        fontsize=8.5,
        color=INK2,
        va="top",
    )


def _line_axes(figsize=(9.5, 5.2)):
    fig, ax = plt.subplots(figsize=figsize)
    fig.subplots_adjust(left=0.08, right=0.86, top=0.78, bottom=0.14)
    return fig, ax


def _heat_grid(ax, n_rows: int, n_cols: int) -> None:
    """2px surface gaps between heatmap cells, no spines."""
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks(np.arange(n_cols) - 0.5, minor=True)
    ax.set_yticks(np.arange(n_rows) - 0.5, minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="minor", length=0)


# ── data access ──────────────────────────────────────────────────────────────
class Data:
    """DuckDB views over every marketplace with built panels.

    Each marketplace's final month is dropped from the panel views: the data
    was pulled part-way through it, so its counts would be artificially low.
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.con = duckdb.connect()
        self.con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}'")
        self.con.execute(f"SET threads={settings.duckdb_threads}")
        self.con.execute(
            f"CREATE VIEW universe AS SELECT asin FROM read_parquet('{settings.universe_path.as_posix()}')"
        )
        self.n_universe = self.con.execute("SELECT count(*) FROM universe").fetchone()[0]

        self.locales = [m for m in settings.marketplaces if (settings.panels_dir(m) / "monthly_obs").is_dir()]
        if not self.locales:
            raise SystemExit("no built panels found; run `python -m amsense panels` first")
        COLORS.clear()
        COLORS.update(zip(self.locales, PALETTE, strict=False))
        self.lines = self.locales[: len(PALETTE)]  # marketplaces drawn as lines
        self.omitted = self.locales[len(PALETTE) :]
        if self.omitted:
            log.warning("line charts show %d marketplaces; left out: %s", len(PALETTE), self.omitted)
        for p in ("monthly_obs", "monthly_locf"):
            union = " UNION ALL BY NAME ".join(
                f"SELECT '{m}' AS mkt, * FROM read_parquet('{(settings.panels_dir(m) / p).as_posix()}/*.parquet')"
                for m in self.locales
            )
            self.con.execute(f"CREATE VIEW {p}_all AS {union}")
        self.con.execute("CREATE TABLE last_month AS SELECT mkt, max(period) AS pmax FROM monthly_obs_all GROUP BY mkt")
        for p in ("monthly_obs", "monthly_locf"):
            self.con.execute(
                f"CREATE VIEW {p} AS SELECT a.* FROM {p}_all a JOIN last_month l USING (mkt) WHERE a.period < l.pmax"
            )

        cov = {}
        for m in settings.marketplaces:
            try:
                cov[m] = valid_parts(settings.keepa_dir(m) / "coverage", quiet=True)
            except FileNotFoundError:
                continue
        self.con.execute(
            "CREATE VIEW coverage AS "
            + " UNION ALL ".join(f"SELECT '{m}' AS mkt, asin, status FROM read_parquet({f!r})" for m, f in cov.items())
        )
        queried = self.df("SELECT mkt, count(DISTINCT asin) AS n FROM coverage JOIN universe USING (asin) GROUP BY mkt")
        queried = queried.set_index("mkt")["n"] / self.n_universe
        self.queried = queried
        # marketplaces collected far enough to compare coverage across countries
        self.complete = [m for m in settings.marketplaces if queried.get(m, 0) >= MIN_QUERIED]
        log.info("share of universe queried per marketplace:\n%s", queried.round(3).to_string())

        # price = Amazon's own offer, else the lowest new third-party offer; penny listings excluded
        self.price_sql = (
            "CASE WHEN price_amazon IS NOT NULL AND NOT price_amazon_is_penny THEN price_amazon "
            "WHEN price_new IS NOT NULL AND NOT price_new_is_penny THEN price_new END"
        )

    def df(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).fetchdf()

    def lines_note(self) -> str:
        if not self.omitted:
            return ""
        return "\nNot drawn (8-line limit): " + ", ".join(m.upper() for m in self.omitted) + "."


# ── figures ──────────────────────────────────────────────────────────────────
def fig_coverage(d: Data) -> None:
    ref = d.s.reference_marketplace
    df = d.df("""
        SELECT mkt, count(DISTINCT asin) AS queried,
               count(DISTINCT asin) FILTER (WHERE status = 'ok') AS with_history
        FROM coverage JOIN universe USING (asin) GROUP BY mkt
    """).set_index("mkt")
    df["share"] = df["with_history"] / df["queried"]
    log.info("coverage:\n%s", df.to_string())
    df = df.loc[[m for m in d.complete if m != ref]].sort_values("share")

    fig, ax = plt.subplots(figsize=(9, 4))
    fig.subplots_adjust(left=0.17, right=0.95, top=0.76, bottom=0.16)
    y = np.arange(len(df))
    ax.set_axisbelow(True)
    ax.barh(y, df["share"], height=0.5, color=PALETTE[0])
    for yi, (_, r) in zip(y, df.iterrows(), strict=True):
        ax.text(r["share"] + 0.012, yi, f"{r['share']:.0%}  ({r['with_history']:,.0f})", va="center", fontsize=9)
    ax.set_yticks(y, [mkt.get(m).country for m in df.index], color=INK)
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.spines["bottom"].set_visible(False)
    _header(
        fig,
        "One product list, queried in every store",
        f"Share of the {d.n_universe:,} {ref.upper()}-listed ASINs that have a Keepa history in each marketplace",
    )
    _note(fig, "ASINs Keepa does not know in a marketplace cost 0 tokens, so probing another country is cheap.")
    _save(fig, d.s, "coverage")


def fig_overlap(d: Data) -> None:
    locs = d.complete
    df = d.df("""
        WITH ok AS (SELECT DISTINCT mkt, asin FROM coverage JOIN universe USING (asin) WHERE status = 'ok')
        SELECT a.mkt AS m1, b.mkt AS m2, count(*) AS n FROM ok a JOIN ok b USING (asin) GROUP BY 1, 2
    """)
    mat = df.pivot(index="m1", columns="m2", values="n").reindex(index=locs, columns=locs) / d.n_universe
    n_all = d.con.execute(
        f"""SELECT count(*) FROM (SELECT asin FROM coverage JOIN universe USING (asin)
            WHERE status = 'ok' AND mkt IN ({", ".join(f"'{m}'" for m in locs)})
            GROUP BY asin HAVING count(DISTINCT mkt) = {len(locs)})"""
    ).fetchone()[0]
    log.info("overlap (share of universe):\n%s\nin all %d: %s", mat.round(3).to_string(), len(locs), n_all)

    fig, ax = plt.subplots(figsize=(7, 6.4))
    fig.subplots_adjust(left=0.12, right=0.95, top=0.74, bottom=0.1)
    ax.imshow(mat.values, cmap=SEQ, vmin=0, vmax=1)
    for i in range(len(locs)):
        for j in range(len(locs)):
            v = mat.values[i, j]
            ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=10, color="white" if v > 0.55 else INK)
    names = [m.upper() for m in locs]
    ax.set_xticks(range(len(locs)), names, color=INK)
    ax.set_yticks(range(len(locs)), names, color=INK)
    ax.xaxis.tick_top()
    _heat_grid(ax, len(locs), len(locs))
    _header(
        fig,
        "How many products two stores share",
        "Share of the universe with Keepa history in both marketplaces.\nThe diagonal is the marketplace on its own.",
    )
    _note(fig, f"{n_all:,} ASINs ({n_all / d.n_universe:.0%}) have history in all {len(locs)} marketplaces.")
    _save(fig, d.s, "overlap")


def fig_tracked(d: Data) -> None:
    df = d.df(f"""
        SELECT mkt, period, count(DISTINCT asin) AS n FROM monthly_obs
        WHERE ({d.price_sql}) IS NOT NULL AND period >= DATE '2014-01-01'
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    wide = df.pivot(index="period", columns="mkt", values="n")[d.lines]
    wide.index = pd.to_datetime(wide.index)
    log.info("ASINs with a recorded price change, Septembers:\n%s", wide[wide.index.month == 9].to_string())

    fig, ax = _line_axes()
    for m in d.lines:
        ax.plot(wide.index, wide[m], color=COLORS[m])
    top = wide.max().max()
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:,.0f}K"))
    ax.set_ylim(0, top * 1.08)
    _mcauley_line(ax)
    _end_labels(ax, {m: wide[m] for m in d.lines}, lambda v: f"{v / 1000:,.0f}K", min_gap=top * 0.045)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    _header(
        fig,
        "Keepa history runs years past the McAuley cutoff",
        "ASINs with at least one recorded price change in the month",
        d.lines,
    )
    _note(
        fig,
        "Products enter through a 2023 review, so counts rise into 2023 and then fall as products are delisted.\n"
        "Keepa records a point when a value changes; months without a change are not counted here." + d.lines_note(),
    )
    _save(fig, d.s, "tracked")


def fig_reviews(d: Data) -> None:
    df = d.df("""
        WITH base AS (
          SELECT mkt, asin, review_count AS rc0 FROM monthly_locf
          WHERE period = DATE '2023-09-01' AND review_count >= 10
        )
        SELECT l.mkt, l.period, median(l.review_count / b.rc0) - 1 AS idx, count(*) AS n
        FROM monthly_locf l JOIN base b USING (mkt, asin)
        WHERE l.period >= DATE '2023-09-01'
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    wide = df.pivot(index="period", columns="mkt", values="idx")[d.lines]
    wide.index = pd.to_datetime(wide.index)
    base_n = df[df.period == df.period.min()].set_index("mkt")["n"]
    log.info(
        "median review_count growth since Sep-2023:\n%s\nASINs per marketplace:\n%s", wide.iloc[::6].round(3), base_n
    )

    fig, ax = _line_axes()
    for m in d.lines:
        ax.plot(wide.index, wide[m], color=COLORS[m])
    top = wide.max().max()
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set_ylim(0, top * 1.1)
    _end_labels(ax, {m: wide[m] for m in d.lines}, lambda v: f"+{v:.0%}", min_gap=top * 0.045)
    ax.xaxis.set_major_locator(mdates.MonthLocator((1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    _header(
        fig,
        "Reviews keep arriving after the McAuley snapshot",
        "Median growth in review count since September 2023, for products with 10+ reviews then",
        d.lines,
    )
    _note(
        fig,
        f"{int(base_n.sum()):,} ASIN-marketplace pairs. Amazon shares reviews across some storefronts."
        + d.lines_note(),
    )
    _save(fig, d.s, "reviews")


def fig_price_gap(d: Data) -> None:
    if not d.s.fx_path.exists():
        log.warning("price_gap skipped: no FX table at %s (run `python -m amsense fx`)", d.s.fx_path)
        return
    ref = d.s.reference_marketplace
    df = d.df(f"""
        WITH fx AS (SELECT month::DATE AS period, currency, usd_per_unit FROM read_parquet('{d.s.fx_path.as_posix()}')),
        p AS (
          SELECT o.mkt, o.asin, o.period, ({d.price_sql}) * fx.usd_per_unit AS usd
          FROM monthly_obs o JOIN fx ON fx.period = o.period AND fx.currency = o.currency
          WHERE ({d.price_sql}) IS NOT NULL
        )
        SELECT x.mkt, x.period, median(ln(x.usd / r.usd)) AS med_log, count(*) AS n
        FROM p x JOIN p r ON r.asin = x.asin AND r.period = x.period AND r.mkt = '{ref}'
        WHERE x.mkt <> '{ref}' AND x.period >= DATE '2019-01-01'
        GROUP BY 1, 2 HAVING count(*) >= 500 ORDER BY 1, 2
    """)
    df["gap"] = np.exp(df["med_log"]) - 1
    others = [m for m in d.lines if m != ref and m in set(df["mkt"])]
    wide = df.pivot(index="period", columns="mkt", values="gap")[others]
    wide.index = pd.to_datetime(wide.index)
    log.info(
        "median premium vs %s (USD):\n%s\nmatched ASIN-months, last month:\n%s",
        ref,
        wide.iloc[::12].round(3).to_string(),
        df.groupby("mkt")["n"].last().to_string(),
    )

    fig, ax = _line_axes()
    ax.axhline(0, color=BASE, lw=1)
    for m in others:
        ax.plot(wide.index, wide[m], color=COLORS[m])
    hi = wide.max().max()
    ax.set_ylim(0, hi * 1.1)
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    _mcauley_line(ax)
    _end_labels(ax, {m: wide[m] for m in others}, lambda v: f"{v:+.0%}", min_gap=hi * 0.05)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    _header(
        fig,
        f"The same product costs more outside the {ref.upper()}",
        f"Median price premium over the {mkt.get(ref).country} for the identical ASIN in the same month, in USD",
        others,
    )
    _note(
        fig,
        "Monthly Bank of Canada FX. Outside the US and Canada, listed prices include VAT or its local equivalent;\n"
        "US and Canadian prices exclude sales tax. Price = Amazon's own offer, else the lowest new offer; "
        "penny listings excluded." + d.lines_note(),
    )
    _save(fig, d.s, "price_gap")


def fig_events(d: Data, year: int = 2024, cut: float = 0.10) -> None:
    """Daily count of >= `cut` price drops on Amazon's own offer, relative to
    that marketplace's median day, from the raw daily table (month-end panels
    would hide one-day events)."""
    start, end = f"{year}-01-01", f"{year + 1}-01-01"
    frames = []
    for m in d.locales:
        rules = cleaning.rules_for(mkt.get(m), d.s)
        parts = valid_parts(d.s.keepa_dir(m) / "daily", quiet=True)
        frames.append(
            d.df(f"""
            WITH x AS (
              SELECT asin, timestamp, value FROM read_parquet({parts!r}) d
              WHERE series = 'amazon' AND {cleaning.daily_where_sql(rules, "d")}
                AND timestamp >= epoch_ms(TIMESTAMP '{start}' - INTERVAL 90 DAY)
                AND timestamp < epoch_ms(TIMESTAMP '{end}')
                AND asin IN (SELECT asin FROM universe)
            ),
            c AS (
              SELECT epoch_ms(timestamp)::DATE AS day, value,
                     lag(value) OVER (PARTITION BY asin ORDER BY timestamp) AS prev
              FROM x
            )
            SELECT '{m}' AS mkt, day, count(*) FILTER (WHERE value <= (1 - {cut}) * prev) AS cuts
            FROM c WHERE day >= DATE '{start}' GROUP BY day ORDER BY day
        """)
        )
    df = pd.concat(frames)
    df["day"] = pd.to_datetime(df["day"])
    df["idx"] = df["cuts"] / df.groupby("mkt")["cuts"].transform("median")
    for m, g in df.groupby("mkt"):
        top = g.nlargest(4, "idx")
        log.info("[%s] top cut days: %s", m, ", ".join(f"{r.day:%b %d} ({r.idx:.1f}x)" for r in top.itertuples()))

    n = len(d.locales)
    ncols = 2
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(10, 1.9 * nrows + 1.8), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.06, right=0.97, top=0.77, bottom=0.08, hspace=0.3, wspace=0.08)
    ymax = df["idx"].max() * 1.08
    for ax, m in zip(axes.flat, d.locales, strict=False):
        g = df[df.mkt == m]
        for _, a, b in EVENTS_2024:
            ax.axvspan(pd.Timestamp(a), pd.Timestamp(b) + pd.Timedelta(days=1), color=BAND, lw=0, zorder=0)
        ax.axhline(1, color=BASE, lw=1, zorder=1)
        ax.plot(g["day"], g["idx"], color=COLORS.get(m, PALETTE[0]), lw=1.4)
        ax.text(0.01, 0.97, mkt.get(m).country, transform=ax.transAxes, fontsize=10, color=INK, va="top")
        ax.set_ylim(0, ymax)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}x"))
        ax.xaxis.set_major_locator(mdates.MonthLocator((1, 4, 7, 10)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    for ax in list(axes.flat)[n:]:
        ax.set_visible(False)
    for ax in axes[0]:
        for i, (label, a, _) in enumerate(EVENTS_2024):
            ax.text(
                pd.Timestamp(a),
                1.03 + 0.12 * (i % 2),
                label,
                transform=ax.get_xaxis_transform(),
                fontsize=7.5,
                color=INK2,
                ha="center",
                va="bottom",
            )
    _header(
        fig,
        f"Sale events show up as one-day spikes in price cuts ({year})",
        f"Daily number of Amazon price cuts of {cut:.0%} or more, relative to each marketplace's median day",
    )
    _note(fig, "Shaded: Amazon sale events (US dates). Computed from the raw daily table, not the month-end panels.")
    _save(fig, d.s, "events")


FIGURES: dict[str, Callable[[Data], None]] = {
    "coverage": fig_coverage,
    "overlap": fig_overlap,
    "tracked": fig_tracked,
    "reviews": fig_reviews,
    "price_gap": fig_price_gap,
    "events": fig_events,
}


def make_all(settings: Settings, only: list[str] | None = None) -> None:
    d = Data(settings)
    log.info("marketplaces with panels: %s; complete enough for coverage figures: %s", d.locales, d.complete)
    for name, fn in FIGURES.items():
        if not only or name in only:
            fn(d)
