"""PyArrow schemas for the tables the Keepa collector writes.

All timestamps are Unix milliseconds (int64). Every table is keyed on the child
`asin`, the level at which Keepa tracks price and rating and the key McAuley
reviews use, so Keepa output joins back to McAuley on `asin`.

  * KEEPA_HISTORY_SCHEMA  - full resolution, one row per (asin, series, point)
  * KEEPA_DAILY_SCHEMA    - last observation per (asin, series, UTC day)
  * KEEPA_SNAPSHOT_SCHEMA - latest value per ASIN, using McAuley column names
  * KEEPA_PRODUCT_SCHEMA  - static product attributes, one row per ASIN
  * KEEPA_COVERAGE_SCHEMA - one row per requested ASIN (found / empty / not_found)
"""

import pyarrow as pa

# Long-format historical panel: one row per (asin, series, timestamp).
KEEPA_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("asin", pa.string()),  # child/variation ASIN (Keepa key level)
        pa.field("parent_asin", pa.string()),  # Keepa parentAsin (linkage; may be null)
        # amazon | new | used | sales_rank | rating | review_count
        pa.field("series", pa.string()),
        pa.field("timestamp", pa.int64()),  # Unix milliseconds
        pa.field("value", pa.float64()),  # price (local currency) / rank / stars(0-5) / count
        pa.field("crawl_ts", pa.int64()),
        pa.field("run_id", pa.string()),
        pa.field("source", pa.string()),
    ]
)

# Daily panel: last observation per (asin, series, UTC day). Derived from the
# full-resolution rows; ~10-20x smaller and analysis-ready for panel work.
KEEPA_DAILY_SCHEMA = pa.schema(
    [
        pa.field("asin", pa.string()),
        pa.field("parent_asin", pa.string()),
        pa.field("series", pa.string()),
        pa.field("date", pa.string()),  # YYYY-MM-DD (UTC)
        pa.field("timestamp", pa.int64()),  # ms of the last observation that day
        pa.field("value", pa.float64()),
        pa.field("crawl_ts", pa.int64()),
        pa.field("run_id", pa.string()),
        pa.field("source", pa.string()),
    ]
)

# Latest value per ASIN. Field names reuse McAuley metadata names where they
# line up (average_rating, rating_number, price) so this joins onto that schema.
KEEPA_SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("asin", pa.string()),
        pa.field("parent_asin", pa.string()),
        pa.field("average_rating", pa.float64()),  # 0-5 stars
        pa.field("rating_number", pa.int64()),
        pa.field("price", pa.float64()),  # current price, local currency
        pa.field("sales_rank", pa.int64()),
        pa.field("sales_rank_category", pa.string()),  # Keepa category node id
        pa.field("crawl_ts", pa.int64()),
        pa.field("run_id", pa.string()),
        pa.field("source", pa.string()),
    ]
)

# Per-ASIN audit / data-range record.
# One row per requested ASIN, so gaps are explicit.
KEEPA_COVERAGE_SCHEMA = pa.schema(
    [
        pa.field("asin", pa.string()),
        pa.field("parent_asin", pa.string()),
        pa.field("found_in_keepa", pa.bool_()),
        pa.field("n_points_price", pa.int64()),
        pa.field("n_points_sales_rank", pa.int64()),
        pa.field("n_points_rating", pa.int64()),
        pa.field("n_points_review_count", pa.int64()),
        pa.field("earliest_ts", pa.int64()),  # min ts across all series
        pa.field("latest_ts", pa.int64()),  # max ts across all series
        pa.field("first_date", pa.string()),  # ISO date of earliest_ts
        pa.field("last_date", pa.string()),  # ISO date of latest_ts
        pa.field("tokens_consumed", pa.int64()),  # -1 = unknown (batch-level)
        pa.field("status", pa.string()),  # ok | not_found | error
        pa.field("error_msg", pa.string()),
        pa.field("crawled_at", pa.int64()),
        pa.field("run_id", pa.string()),
    ]
)

# Static (non-time-series) product attributes returned for free by the product
# endpoint. One row per ASIN. Lists/dicts stored as JSON strings; Keepa-time
# fields converted to Unix ms; current per-subcategory ranks in JSON.
KEEPA_PRODUCT_SCHEMA = pa.schema(
    [
        pa.field("asin", pa.string()),
        pa.field("parent_asin", pa.string()),
        pa.field("title", pa.string()),
        pa.field("brand", pa.string()),
        pa.field("manufacturer", pa.string()),
        pa.field("model", pa.string()),
        pa.field("part_number", pa.string()),
        pa.field("product_group", pa.string()),
        pa.field("product_type", pa.int64()),  # 0 std, 5 variation parent, ...
        pa.field("type", pa.string()),
        pa.field("item_type_keyword", pa.string()),
        pa.field("color", pa.string()),
        pa.field("size", pa.string()),
        pa.field("material", pa.string()),
        pa.field("binding", pa.string()),
        pa.field("author", pa.string()),
        pa.field("number_of_items", pa.int64()),
        pa.field("number_of_pages", pa.int64()),
        pa.field("package_quantity", pa.int64()),
        pa.field("item_weight", pa.int64()),
        pa.field("item_height", pa.int64()),
        pa.field("item_length", pa.int64()),
        pa.field("item_width", pa.int64()),
        pa.field("package_weight", pa.int64()),
        pa.field("root_category", pa.int64()),
        pa.field("sales_rank_reference", pa.int64()),
        pa.field("website_display_group", pa.string()),
        pa.field("sales_rank_display_group", pa.string()),
        pa.field("brand_store_name", pa.string()),
        pa.field("referral_fee_percent", pa.int64()),
        pa.field("availability_amazon", pa.int64()),
        pa.field("is_adult_product", pa.bool_()),
        pa.field("has_reviews", pa.bool_()),
        pa.field("launchpad", pa.bool_()),
        pa.field("is_redirect_asin", pa.bool_()),
        pa.field("listed_since", pa.int64()),  # Unix ms
        pa.field("tracking_since", pa.int64()),
        pa.field("last_update", pa.int64()),
        pa.field("last_price_change", pa.int64()),
        pa.field("last_rating_update", pa.int64()),
        pa.field("categories", pa.string()),  # JSON list
        pa.field("category_tree", pa.string()),  # JSON list of {catId,name}
        pa.field("features", pa.string()),  # JSON list
        pa.field("description", pa.string()),
        pa.field("images", pa.string()),  # JSON list
        pa.field("upc_list", pa.string()),  # JSON list
        pa.field("ean_list", pa.string()),  # JSON list
        pa.field("gtin_list", pa.string()),  # JSON list
        pa.field("variations", pa.string()),  # JSON list of child variations
        pa.field("fba_fees", pa.string()),  # JSON
        pa.field("unit_count", pa.string()),  # JSON
        pa.field("current_sales_ranks", pa.string()),  # JSON {categoryId: rank}
        pa.field("crawl_ts", pa.int64()),
        pa.field("run_id", pa.string()),
        pa.field("source", pa.string()),
    ]
)
