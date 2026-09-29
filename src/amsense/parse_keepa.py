"""Pure transforms: one raw Keepa product JSON -> history / snapshot / coverage rows.

We hit the Keepa REST API directly, so we decode the raw ``csv`` arrays here
ourselves rather than relying on a wrapper library.

Keepa ``product["csv"]`` is a list indexed by *CsvType*. Each entry is either
``None`` (that metric is not tracked) or a flat, chronologically ordered array
``[keepaMinute, value, keepaMinute, value, ...]``. Sentinel value ``-1`` means
"no data at this point" and is dropped.

Scaling by series:
  * prices: integers in the locale's smallest currency unit -> /price_divisor
    (100 for cents; 1 for yen, per Keepa's api_backend Product.java)
  * rating: integer **0-50** -> /10.0 stars (0-5)
  * ranks / offer counts: raw integer

We collect every csv series that Keepa returns **without** the paid `offers`/
`buybox` parameter (CsvType indices whose "requires offers" flag is false), using
the canonical Keepa CsvType index->name mapping. Shipping-inclusive / buy-box
series (7,9,10,18-27,31,32,33) need `offers` and are excluded. EXTRA_INFO(15) is
an internal update marker and is skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# Keepa time is "minutes since the Keepa epoch". unix_ms = (kmin + offset) * 60000
KEEPA_EPOCH_MINUTES = 21_564_000

# CsvType index -> canonical series name, grouped by unit handling.
# Only free (no-offers) indices; canonical per Keepa's CsvType enum.
PRICE_SERIES = {  # integer minor units -> currency units
    0: "amazon",
    1: "new",
    2: "used",
    4: "list_price",
    5: "collectible",
    6: "refurbished",
    8: "lightning_deal",
    28: "ebay_new",
    29: "ebay_used",
    30: "trade_in",
}
COUNT_SERIES = {  # raw integer offer/review counts
    11: "count_new",
    12: "count_used",
    13: "count_refurbished",
    14: "count_collectible",
    17: "review_count",
    34: "count_new_fba",
    35: "count_new_fbm",
}
RATING_SERIES = {16: "rating"}  # 0-50 -> 0-5 stars
# NOTE: sales_rank is NOT taken from csv[3] (the primary-category rank, which is
# empty for the ~68% of products with salesRankReference == -1). Instead we source
# it from the salesRanks dict (per-subcategory histories) via _choose_rank_series,
# which raises rank coverage from ~56% to ~92%.
SERIES_INDEX: dict[int, str] = {**PRICE_SERIES, **RATING_SERIES, **COUNT_SERIES}

# The four "headline" variables the coverage audit counts (extra series are
# collected into the panels but don't get their own coverage columns).
CORE_PRICE = {"amazon", "new", "used"}

# Product-object fields stored as Keepa-time minutes -> converted to Unix ms.
_KEEPA_TIME_FIELDS = (
    "listedSince",
    "trackingSince",
    "lastUpdate",
    "lastPriceChange",
    "lastRatingUpdate",
    "lastEbayUpdate",
)


def keepa_minute_to_unix_ms(kmin: int) -> int:
    """Convert a Keepa-time minute to Unix milliseconds (int64, 13 digits)."""
    return (int(kmin) + KEEPA_EPOCH_MINUTES) * 60_000


def _scale(series: str, raw: int, price_divisor: float = 100.0) -> float:
    """Apply per-series unit scaling to a raw csv value."""
    if series in PRICE_SERIES.values():
        return raw / price_divisor
    if series == "rating":
        return raw / 10.0
    # ranks and counts: raw integer
    return float(raw)


def _iter_points(arr):
    """Yield (keepaMinute, rawValue) pairs from a flat [t, v, t, v, ...] array,
    skipping the -1 "no data" sentinel on the value."""
    if not arr:
        return
    for i in range(0, len(arr) - 1, 2):
        kmin = arr[i]
        val = arr[i + 1]
        if val == -1:
            continue
        yield kmin, val


def _iso_date(unix_ms: int | None) -> str | None:
    if unix_ms is None:
        return None
    return datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _choose_rank_series(product: dict):
    """Pick one representative sales-rank series -> (category_id, [t,rank,...]).

    csv[3] (the reference-category rank) is empty for the ~68% of products with
    salesRankReference == -1, so we prefer the salesRanks dict: the reference
    category, then the root category, then the most-tracked category. Falls back
    to csv[3] only when no salesRanks dict is present."""
    sr = product.get("salesRanks") or {}
    ref = product.get("salesRankReference")
    root = product.get("rootCategory")
    if sr:
        for cand in (ref, root):
            if cand not in (None, -1) and str(cand) in sr and sr[str(cand)]:
                return int(cand), sr[str(cand)]
        cat, arr = max(sr.items(), key=lambda kv: len(kv[1]) if kv[1] else 0)
        return int(cat), arr
    csv = product.get("csv") or []
    arr = csv[3] if len(csv) > 3 else None
    return (int(ref) if ref not in (None, -1) else None), arr


def parse_product(product: dict, run_id: str, crawl_ts: int, price_divisor: float = 100.0):
    """Decode one Keepa product dict.

    Returns ``(history_rows, snapshot_row, coverage_row)``. ``snapshot_row`` is
    ``None`` only if the product carried no usable data at all (still produces a
    coverage row so the ASIN is accounted for).
    """
    asin = product.get("asin")
    parent_asin = product.get("parentAsin")  # Keepa's parent (linkage to McAuley)
    csv = product.get("csv") or []

    history_rows: list[dict] = []
    counts = {"price": 0, "sales_rank": 0, "rating": 0, "review_count": 0}
    latest = {}  # series -> (unix_ms, scaled_value)
    all_ts: list[int] = []

    for idx, series in SERIES_INDEX.items():
        arr = csv[idx] if idx < len(csv) else None
        for kmin, raw in _iter_points(arr):
            ts = keepa_minute_to_unix_ms(kmin)
            value = _scale(series, raw, price_divisor)
            history_rows.append(
                {
                    "asin": asin,
                    "parent_asin": parent_asin,
                    "series": series,
                    "timestamp": ts,
                    "value": value,
                    "crawl_ts": crawl_ts,
                    "run_id": run_id,
                    "source": "keepa_api",
                }
            )
            all_ts.append(ts)
            # coverage counts only the four headline variables
            if series in CORE_PRICE:
                counts["price"] += 1
            elif series in ("sales_rank", "rating", "review_count"):
                counts[series] += 1
            # track latest point per series for the snapshot
            prev = latest.get(series)
            if prev is None or ts >= prev[0]:
                latest[series] = (ts, value)

    # --- sales_rank: sourced from salesRanks (see _choose_rank_series) ------
    rank_cat, rank_arr = _choose_rank_series(product)
    for kmin, raw in _iter_points(rank_arr):
        ts = keepa_minute_to_unix_ms(kmin)
        history_rows.append(
            {
                "asin": asin,
                "parent_asin": parent_asin,
                "series": "sales_rank",
                "timestamp": ts,
                "value": float(raw),
                "crawl_ts": crawl_ts,
                "run_id": run_id,
                "source": "keepa_api",
            }
        )
        all_ts.append(ts)
        counts["sales_rank"] += 1
        prev = latest.get("sales_rank")
        if prev is None or ts >= prev[0]:
            latest["sales_rank"] = (ts, float(raw))

    # --- snapshot: latest value of each variable ---------------------------
    def latest_val(series):
        return latest[series][1] if series in latest else None

    # current "price" = latest Amazon price, else latest marketplace-new price
    price = latest_val("amazon")
    if price is None:
        price = latest_val("new")

    avg_rating = latest_val("rating")
    review_count = latest_val("review_count")
    sales_rank = latest_val("sales_rank")

    snapshot_row = {
        "asin": asin,
        "parent_asin": parent_asin,
        "average_rating": avg_rating,
        "rating_number": int(review_count) if review_count is not None else None,
        "price": price,
        "sales_rank": int(sales_rank) if sales_rank is not None else None,
        "sales_rank_category": str(rank_cat) if rank_cat is not None else None,
        "crawl_ts": crawl_ts,
        "run_id": run_id,
        "source": "keepa_api",
    }

    # No usable metrics at all (e.g. Keepa has the ASIN but every point is -1)
    # -> don't emit a hollow snapshot row; the coverage row still accounts for it.
    if price is None and avg_rating is None and review_count is None and sales_rank is None:
        snapshot_row = None

    earliest_ts = min(all_ts) if all_ts else None
    latest_ts = max(all_ts) if all_ts else None
    coverage_row = {
        "asin": asin,
        "parent_asin": parent_asin,
        "found_in_keepa": True,
        "n_points_price": counts["price"],
        "n_points_sales_rank": counts["sales_rank"],
        "n_points_rating": counts["rating"],
        "n_points_review_count": counts["review_count"],
        "earliest_ts": earliest_ts,
        "latest_ts": latest_ts,
        "first_date": _iso_date(earliest_ts),
        "last_date": _iso_date(latest_ts),
        "tokens_consumed": -1,  # filled in per-batch by the driver if available
        "status": "ok" if all_ts else "empty",
        "error_msg": "",
        "crawled_at": crawl_ts,
        "run_id": run_id,
    }

    return history_rows, snapshot_row, coverage_row


def downsample_daily(history_rows: list[dict]) -> list[dict]:
    """Collapse full-resolution history to one row per (asin, series, UTC day):
    the last observation of that day. Pure function over history rows."""
    best: dict[tuple, dict] = {}
    for r in history_rows:
        date = _iso_date(r["timestamp"])
        key = (r["asin"], r["series"], date)
        cur = best.get(key)
        if cur is None or r["timestamp"] >= cur["timestamp"]:
            best[key] = r
    daily = []
    for (asin, series, date), r in best.items():
        daily.append(
            {
                "asin": asin,
                "parent_asin": r["parent_asin"],
                "series": series,
                "date": date,
                "timestamp": r["timestamp"],
                "value": r["value"],
                "crawl_ts": r["crawl_ts"],
                "run_id": r["run_id"],
                "source": r["source"],
            }
        )
    return daily


def not_found_coverage(
    asin: str,
    run_id: str,
    crawl_ts: int,
    parent_asin: str | None = None,
    status: str = "not_found",
    error_msg: str = "",
) -> dict:
    """Coverage row for an ASIN Keepa returned no product for (0 tokens under
    update=-1), or that errored. Keeps every requested ASIN accounted for."""
    return {
        "asin": asin,
        "parent_asin": parent_asin,
        "found_in_keepa": False,
        "n_points_price": 0,
        "n_points_sales_rank": 0,
        "n_points_rating": 0,
        "n_points_review_count": 0,
        "earliest_ts": None,
        "latest_ts": None,
        "first_date": None,
        "last_date": None,
        "tokens_consumed": 0,
        "status": status,
        "error_msg": error_msg,
        "crawled_at": crawl_ts,
        "run_id": run_id,
    }


def parse_product_attributes(product: dict, run_id: str, crawl_ts: int) -> dict:
    """One row per ASIN of the free (no-offers) static product attributes.
    Lists/dicts are JSON-encoded; Keepa-time fields are converted to Unix ms.
    Also captures the current per-subcategory ranks from salesRanks."""

    def jd(v):
        return json.dumps(v, ensure_ascii=False) if v not in (None, [], {}) else None

    def kt(field):
        v = product.get(field)
        return keepa_minute_to_unix_ms(v) if isinstance(v, int) and v not in (0, -1) else None

    # current rank in each subcategory (last non -1 value of each salesRanks series)
    current_ranks = {}
    for cat, arr in (product.get("salesRanks") or {}).items():
        if not arr:
            continue
        for j in range(len(arr) - 1, 0, -2):
            if arr[j] != -1:
                current_ranks[str(cat)] = arr[j]
                break

    return {
        "asin": product.get("asin"),
        "parent_asin": product.get("parentAsin"),
        "title": product.get("title"),
        "brand": product.get("brand"),
        "manufacturer": product.get("manufacturer"),
        "model": product.get("model"),
        "part_number": product.get("partNumber"),
        "product_group": product.get("productGroup"),
        "product_type": product.get("productType"),
        "type": product.get("type"),
        "item_type_keyword": product.get("itemTypeKeyword"),
        "color": product.get("color"),
        "size": product.get("size"),
        "material": product.get("material"),
        "binding": product.get("binding"),
        "author": product.get("author"),
        "number_of_items": product.get("numberOfItems"),
        "number_of_pages": product.get("numberOfPages"),
        "package_quantity": product.get("packageQuantity"),
        "item_weight": product.get("itemWeight"),
        "item_height": product.get("itemHeight"),
        "item_length": product.get("itemLength"),
        "item_width": product.get("itemWidth"),
        "package_weight": product.get("packageWeight"),
        "root_category": product.get("rootCategory"),
        "sales_rank_reference": product.get("salesRankReference"),
        "website_display_group": product.get("websiteDisplayGroupName"),
        "sales_rank_display_group": product.get("salesRankDisplayGroup"),
        "brand_store_name": product.get("brandStoreName"),
        "referral_fee_percent": product.get("referralFeePercent"),
        "availability_amazon": product.get("availabilityAmazon"),
        "is_adult_product": product.get("isAdultProduct"),
        "has_reviews": product.get("hasReviews"),
        "launchpad": product.get("launchpad"),
        "is_redirect_asin": product.get("isRedirectASIN"),
        "listed_since": kt("listedSince"),
        "tracking_since": kt("trackingSince"),
        "last_update": kt("lastUpdate"),
        "last_price_change": kt("lastPriceChange"),
        "last_rating_update": kt("lastRatingUpdate"),
        "categories": jd(product.get("categories")),
        "category_tree": jd(product.get("categoryTree")),
        "features": jd(product.get("features")),
        "description": product.get("description"),
        "images": jd(product.get("images")),
        "upc_list": jd(product.get("upcList")),
        "ean_list": jd(product.get("eanList")),
        "gtin_list": jd(product.get("gtinList")),
        "variations": jd(product.get("variations")),
        "fba_fees": jd(product.get("fbaFees")),
        "unit_count": jd(product.get("unitCount")),
        "current_sales_ranks": jd(current_ranks) if current_ranks else None,
        "crawl_ts": crawl_ts,
        "run_id": run_id,
        "source": "keepa_api",
    }
