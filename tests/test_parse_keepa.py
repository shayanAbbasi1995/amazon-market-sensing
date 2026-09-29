from amsense.collect import rows_from_response
from amsense.parse_keepa import (
    KEEPA_EPOCH_MINUTES,
    downsample_daily,
    keepa_minute_to_unix_ms,
    parse_product,
)

# 2023-01-01 00:00 UTC in Keepa minutes
T0 = 1_672_531_200_000 // 60_000 - KEEPA_EPOCH_MINUTES


def _product(**overrides) -> dict:
    csv = [None] * 36
    csv[0] = [T0, 1999, T0 + 60, -1, T0 + 1440, 2499]  # amazon price, one -1 gap
    csv[16] = [T0, 45]  # rating 4.5
    csv[17] = [T0, 120, T0 + 1440, 130]  # review count
    p = {
        "asin": "B000TEST01",
        "parentAsin": "B000PARENT",
        "csv": csv,
        "salesRanks": {"172282": [T0, 900, T0 + 1440, 850]},
        "salesRankReference": -1,
        "rootCategory": 172282,
    }
    p.update(overrides)
    return p


def test_keepa_epoch_conversion():
    assert keepa_minute_to_unix_ms(T0) == 1_672_531_200_000


def test_sentinel_dropped_and_prices_scaled():
    hist, snap, cov = parse_product(_product(), "run", 0)
    amazon = [r for r in hist if r["series"] == "amazon"]
    assert [r["value"] for r in amazon] == [19.99, 24.99]  # -1 point removed, cents -> units
    assert snap["price"] == 24.99  # latest amazon price
    assert snap["average_rating"] == 4.5
    assert snap["rating_number"] == 130
    assert cov["status"] == "ok" and cov["n_points_price"] == 2


def test_jpy_prices_not_divided():
    hist, _, _ = parse_product(_product(), "run", 0, price_divisor=1.0)
    assert [r["value"] for r in hist if r["series"] == "amazon"] == [1999.0, 2499.0]


def test_sales_rank_from_sales_ranks_when_reference_missing():
    hist, snap, _ = parse_product(_product(), "run", 0)
    ranks = [r["value"] for r in hist if r["series"] == "sales_rank"]
    assert ranks == [900.0, 850.0]
    assert snap["sales_rank"] == 850 and snap["sales_rank_category"] == "172282"


def test_daily_keeps_last_observation_of_day():
    hist = [
        {
            "asin": "A",
            "parent_asin": None,
            "series": "amazon",
            "timestamp": 1_672_531_200_000 + h * 3_600_000,
            "value": float(h),
            "crawl_ts": 0,
            "run_id": "r",
            "source": "keepa_api",
        }
        for h in (1, 5, 3)
    ]
    daily = downsample_daily(hist)
    assert len(daily) == 1 and daily[0]["value"] == 5.0


def test_every_requested_asin_gets_a_coverage_row():
    rows = rows_from_response(["B000TEST01", "B000MISSING"], {"products": [_product()]}, "run", 0, 100.0)
    status = {c["asin"]: c["status"] for c in rows["coverage"]}
    assert status == {"B000TEST01": "ok", "B000MISSING": "not_found"}
    assert len(rows["product"]) == 1 and rows["daily"]
