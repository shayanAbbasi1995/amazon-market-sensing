"""End-to-end panel build on a tiny synthetic marketplace."""

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from amsense import config, panels
from amsense.schemas import KEEPA_DAILY_SCHEMA


def _ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def test_monthly_obs_and_locf(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        "category: All_Beauty\nmarketplaces: [us]\npanels: {batches: 2}\npaths: {data: ./d}\n", encoding="utf-8"
    )
    s = config.load(tmp_path / "config.yaml")

    rows = [
        ("A", "amazon", _ms(2024, 1, 5), 10.0),
        ("A", "amazon", _ms(2024, 1, 20), 12.0),  # last in Jan wins
        ("A", "review_count", _ms(2024, 1, 5), 50),
        ("A", "review_count", _ms(2024, 3, 5), 40),  # glitch dip
        ("A", "amazon", _ms(2024, 3, 1), 0.01),  # penny
        ("B", "amazon", _ms(2024, 2, 1), 5.0),
        ("Z", "amazon", _ms(2024, 2, 1), 7.0),  # not in universe
    ]
    daily_dir = s.keepa_dir("us") / "daily"
    daily_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "asin": a,
                    "parent_asin": None,
                    "series": ser,
                    "date": None,
                    "timestamp": t,
                    "value": float(v),
                    "crawl_ts": 0,
                    "run_id": "r",
                    "source": "keepa_api",
                }
                for a, ser, t, v in rows
            ],
            schema=KEEPA_DAILY_SCHEMA,
        ),
        daily_dir / "part_x_00000.parquet",
    )
    s.universe_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"asin": ["A", "B"], "current_price": [1.0, 1.0]}), s.universe_path)

    panels.build(s, "us", ["monthly_obs", "monthly_locf"])
    con = duckdb.connect()
    root = s.panels_dir("us").as_posix()

    obs = con.execute(f"""SELECT asin, strftime(period, '%Y-%m'), price_amazon, review_count, price_amazon_is_penny
                          FROM read_parquet('{root}/monthly_obs/*.parquet') ORDER BY 1, 2""").fetchall()
    assert obs == [
        ("A", "2024-01", 12.0, 50.0, False),
        ("A", "2024-03", 0.01, 50.0, True),  # review_count clamped to running max
        ("B", "2024-02", 5.0, None, False),
    ]

    locf = con.execute(f"""SELECT strftime(period, '%Y-%m'), price_amazon, marketplace, currency
                           FROM read_parquet('{root}/monthly_locf/*.parquet')
                           WHERE asin = 'A' AND period < DATE '2024-04-01' ORDER BY 1""").fetchall()
    assert locf == [("2024-01", 12.0, "US", "USD"), ("2024-02", 12.0, "US", "USD"), ("2024-03", 0.01, "US", "USD")]
    n_z = con.execute(f"SELECT count(*) FROM read_parquet('{root}/monthly_locf/*.parquet') WHERE asin = 'Z'")
    assert n_z.fetchone()[0] == 0
