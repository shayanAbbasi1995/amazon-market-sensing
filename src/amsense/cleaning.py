"""Cleaning rules for the Keepa panels, shared by every panel and marketplace.

Principles
----------
1. Drop only what is provably not a value. Sentinels, overflow artifacts and
   placeholder listings are removed. Real but awkward observations are kept and
   flagged.
2. Thresholds are set once in USD (``cleaning:`` in config.yaml) and scaled to
   each marketplace's currency, so "implausibly expensive" means the same thing
   in Toronto and in Berlin. The FX table below scales thresholds only; panel
   prices are never converted and stay in local currency.
3. Every panel row carries ``marketplace`` and ``currency``, so a union of
   countries cannot silently mix currencies.

Dropped (cell becomes NULL)
  * timestamps before ``min_date`` or in the future
  * negative values (Keepa's -1 "no data" sentinel)
  * zero prices (``list_price = 0`` means "no MSRP recorded"); never imputed
  * prices at or above the ceiling ($21,474,836.47 int32 overflow, $999,999 listings)
  * review counts above the glitch ceiling

Kept and flagged
  * penny prices (<= ~1 USD), usually $0.01 listings whose real cost sits in a
    shipping charge that the free Keepa series do not include -> ``<col>_is_penny``

Corrected
  * ``review_count`` is a cumulative counter, so it is forced non-decreasing
    with a per-ASIN running max.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from amsense.config import Settings, date_to_ms
from amsense.marketplaces import Marketplace

# Raw Keepa series -> panel column. Shared by every builder so monthly and
# weekly panels cannot diverge. eBay series and FBA/FBM offer counts are
# collected in the raw tables but left out of the panels (too sparse / noisy).
SERIES_MAP: dict[str, str] = {
    "amazon": "price_amazon",
    "new": "price_new",
    "used": "price_used",
    "list_price": "list_price",
    "sales_rank": "sales_rank",
    "rating": "rating",
    "review_count": "review_count",
    "count_new": "count_new",
    "count_used": "count_used",
}
PRICE_SERIES: tuple[str, ...] = ("amazon", "new", "used", "list_price")
PRICE_COLS: tuple[str, ...] = tuple(SERIES_MAP[s] for s in PRICE_SERIES)

# Approximate USD value of one unit of each currency. Used ONLY to scale the
# plausibility thresholds; precision does not matter at that use.
USD_PER_UNIT: dict[str, float] = {
    "USD": 1.00,
    "GBP": 1.27,
    "EUR": 1.09,
    "CAD": 0.73,
    "JPY": 0.0067,
    "INR": 0.012,
    "MXN": 0.055,
    "BRL": 0.19,
}


def to_local(usd_amount: float, currency: str) -> float:
    """Convert a USD threshold into ``currency``, rounded to a readable number."""
    if currency not in USD_PER_UNIT:
        raise SystemExit(f"no FX reference for {currency!r}; add it to USD_PER_UNIT in cleaning.py")
    local = usd_amount / USD_PER_UNIT[currency]
    if local >= 1000:
        return round(local / 500.0) * 500.0
    if local >= 10:
        return float(round(local))
    return round(local, 2)


@dataclass(frozen=True)
class CleaningRules:
    marketplace: str
    currency: str
    min_ts_ms: int
    max_ts_ms: int
    price_ceiling: float
    penny_threshold: float
    review_count_ceiling: int

    def summary(self) -> str:
        c = self.currency
        return (
            f"cleaning rules [{self.marketplace} / {c}]\n"
            f"  price ceiling    : {c} {self.price_ceiling:,.2f} (at/above is dropped)\n"
            f"  zero prices      : dropped -> NULL, never imputed\n"
            f"  penny threshold  : {c} {self.penny_threshold:,.2f} (kept, flagged <col>_is_penny)\n"
            f"  review_count cap : {self.review_count_ceiling:,} (and forced non-decreasing)\n"
            f"  timestamp window : [{self.min_ts_ms}, {self.max_ts_ms}] ms UTC"
        )


def rules_for(m: Marketplace, settings: Settings) -> CleaningRules:
    cfg = settings.cleaning
    return CleaningRules(
        marketplace=m.code,
        currency=m.currency,
        min_ts_ms=date_to_ms(cfg["min_date"]),
        max_ts_ms=int(time.time() * 1000) + 2 * 86_400_000,
        price_ceiling=to_local(float(cfg["price_ceiling_usd"]), m.currency),
        penny_threshold=to_local(float(cfg["penny_threshold_usd"]), m.currency),
        review_count_ceiling=int(cfg["review_count_ceiling"]),
    )


def daily_where_sql(rules: CleaningRules, alias: str = "d") -> str:
    """WHERE predicates for the raw ``daily`` table, one rule per line."""
    a = alias
    prices = ", ".join(f"'{s}'" for s in PRICE_SERIES)
    return "\n  ".join(
        [
            f"{a}.timestamp >= {rules.min_ts_ms}",
            f"AND {a}.timestamp <= {rules.max_ts_ms}",
            f"AND {a}.value >= 0",
            f"AND NOT ({a}.series IN ({prices}) AND {a}.value >= {rules.price_ceiling})",
            f"AND NOT ({a}.series IN ({prices}) AND {a}.value = 0)",
            f"AND NOT ({a}.series = 'review_count' AND {a}.value > {rules.review_count_ceiling})",
        ]
    )


def penny_flag_cols() -> list[str]:
    return [c + "_is_penny" for c in PRICE_COLS]


def penny_flag_exprs(rules: CleaningRules) -> list[str]:
    """A NULL price gives a NULL flag, so the flag never claims anything about
    a cell where no price was observed."""
    return [
        f"CASE WHEN {c} IS NULL THEN NULL ELSE {c} <= {rules.penny_threshold} END AS {c}_is_penny" for c in PRICE_COLS
    ]


def identity_cols_sql(rules: CleaningRules) -> str:
    return f"'{rules.marketplace}' AS marketplace, '{rules.currency}' AS currency"
