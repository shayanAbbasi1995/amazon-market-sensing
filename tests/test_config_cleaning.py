from pathlib import Path

import duckdb
import pytest

from amsense import cleaning, config, marketplaces


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> config.Settings:
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "category: All_Beauty\nmarketplaces: [us, ca, de]\nreference_marketplace: ca\npaths: {data: ./d}\n",
        encoding="utf-8",
    )
    return config.load(cfg)


def test_paths_resolve_from_templates(settings, tmp_path):
    assert settings.keepa_dir("de") == (tmp_path / "d" / "All_Beauty" / "keepa" / "DE").resolve()
    assert settings.panels_dir("us").name == "US"
    assert settings.collection_order == ["ca", "us", "de"]


def test_missing_key_fails_loudly(settings):
    with pytest.raises(SystemExit, match="KEEPA_API_KEY"):
        _ = settings.keepa_api_key


def test_key_read_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("category: All_Beauty\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEEPA_API_KEY=abc123\n", encoding="utf-8")
    assert config.load(tmp_path / "config.yaml").keepa_api_key == "abc123"


def test_unknown_marketplace_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("category: X\nmarketplaces: [us, zz]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="zz"):
        config.load(tmp_path / "config.yaml")


def test_thresholds_scale_to_local_currency(settings):
    ca = cleaning.rules_for(marketplaces.get("ca"), settings)
    us = cleaning.rules_for(marketplaces.get("us"), settings)
    assert us.price_ceiling == 10_000
    assert ca.price_ceiling == 13_500  # 10k USD / 0.73, rounded to 500
    assert ca.currency == "CAD"


def test_where_clause_drops_sentinels_and_placeholders(settings):
    rules = cleaning.rules_for(marketplaces.get("us"), settings)
    ts = rules.min_ts_ms + 1
    con = duckdb.connect()
    con.execute("CREATE TABLE d (asin VARCHAR, series VARCHAR, timestamp BIGINT, value DOUBLE)")
    con.executemany(
        "INSERT INTO d VALUES (?, ?, ?, ?)",
        [
            ("A", "amazon", ts, 19.99),  # kept
            ("A", "amazon", ts, 0.01),  # penny: kept (flagged later)
            ("A", "list_price", ts, 0.0),  # zero price: dropped
            ("A", "used", ts, 21_474_836.47),  # int32 overflow: dropped
            ("A", "sales_rank", ts, -1),  # sentinel: dropped
            ("A", "review_count", ts, 176_000_000),  # glitch: dropped
            ("A", "amazon", rules.min_ts_ms - 1, 5.0),  # before window: dropped
        ],
    )
    kept = con.execute(f"SELECT value FROM d WHERE {cleaning.daily_where_sql(rules)} ORDER BY value").fetchall()
    assert kept == [(0.01,), (19.99,)]
