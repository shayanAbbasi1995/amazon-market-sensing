"""Seed ASIN sources other than McAuley: files, Product Finder and best sellers."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from amsense import asins, config, keepa_client


def _settings(tmp_path: Path, asins_yaml: str, monkeypatch) -> config.Settings:
    monkeypatch.setenv("KEEPA_API_KEY", "testkey")
    (tmp_path / "config.yaml").write_text(
        f"category: my_list\nmarketplaces: [us, de]\npaths: {{data: ./d}}\nasins:\n{asins_yaml}", encoding="utf-8"
    )
    return config.load(tmp_path / "config.yaml")


@pytest.mark.parametrize("fmt", ["txt", "csv", "parquet"])
def test_seed_from_own_file(tmp_path, monkeypatch, fmt):
    ids = ["B000000001", "B000000002", "B000000001", "B000000003"]  # duplicate is dropped
    f = tmp_path / f"list.{fmt}"
    if fmt == "txt":
        f.write_text("\n".join(ids) + "\n\n", encoding="utf-8")
    elif fmt == "csv":
        f.write_text("asin,note\n" + "\n".join(f"{a},x" for a in ids), encoding="utf-8")
    else:
        pq.write_table(pa.table({"asin": ids}), f)
    s = _settings(tmp_path, f"  source: file\n  file: list.{fmt}\n  max_asins: 2\n", monkeypatch)
    assert asins.build_seed(s) == ["B000000001", "B000000002"]
    assert asins.load_seed(s) == ["B000000001", "B000000002"]  # read back from paths.seed
    with pytest.raises(SystemExit, match="already exists"):
        asins.build_seed(s)


class FakeSession:
    """Records requests and answers like the Keepa API."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params, timeout):
        self.calls.append((url, params))

        class R:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        if url.endswith("/bestsellers"):
            return R({"bestSellersList": {"asinList": [f"BS{params['category']}A", f"BS{params['category']}B"]}})
        page = json.loads(params["selection"])["page"]
        return R({"asinList": self.pages[page] if page < len(self.pages) else [], "totalResults": 5})


def test_seed_from_product_finder_pages(tmp_path, monkeypatch):
    (tmp_path / "finder.json").write_text(json.dumps({"rootCategory": ["172282"], "perPage": 2}), encoding="utf-8")
    s = _settings(tmp_path, "  source: keepa_finder\n  finder_selection: finder.json\n", monkeypatch)
    fake = FakeSession([["A1", "A2"], ["A3", "A4"], ["A5"]])
    monkeypatch.setattr(keepa_client.requests, "Session", lambda: fake)
    assert asins.build_seed(s) == ["A1", "A2", "A3", "A4", "A5"]
    url, params = fake.calls[0]
    assert url == "https://api.keepa.com/query" and params["key"] == "testkey" and params["domain"] == 1
    assert json.loads(params["selection"])["rootCategory"] == ["172282"]


def test_seed_from_bestsellers(tmp_path, monkeypatch):
    s = _settings(tmp_path, "  source: keepa_bestsellers\n  bestseller_categories: [111, 222]\n", monkeypatch)
    monkeypatch.setattr(keepa_client.requests, "Session", lambda: FakeSession([]))
    assert asins.build_seed(s) == ["BS111A", "BS111B", "BS222A", "BS222B"]


def test_unknown_source_rejected(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="asins.source"):
        _settings(tmp_path, "  source: scrape_amazon\n", monkeypatch)
