"""Download one category of McAuley Lab's Amazon Reviews 2023 and keep a review window.

Source: https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023
(May 1996 - Sep 2023, 571M reviews, 33 categories).

The raw JSONL files are streamed straight from the Hugging Face Hub, so a
20 GB category never has to fit on disk or in memory:

  pass 1  raw/review_categories/<Category>.jsonl
          keep reviews whose timestamp falls in ``review_window``
          -> reviews.parquet  (child ``asin`` is the Keepa key)
  pass 2  raw/meta_categories/meta_<Category>.jsonl
          keep items whose ``parent_asin`` appeared in pass 1
          -> metadata.parquet

Nested fields (images, videos, details, bought_together) are stored as JSON
strings, because their inner structure varies row to row and would otherwise
break a fixed parquet schema.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem, hf_hub_download

from amsense.config import Settings
from amsense.storage import ParquetAppender

log = logging.getLogger(__name__)

REPO_ID = "McAuley-Lab/Amazon-Reviews-2023"
HF_ROOT = f"datasets/{REPO_ID}"
BATCH = 10_000

REVIEW_SCHEMA = pa.schema(
    [
        ("rating", pa.float64()),
        ("title", pa.string()),
        ("text", pa.string()),
        ("images", pa.string()),
        ("asin", pa.string()),
        ("parent_asin", pa.string()),
        ("user_id", pa.string()),
        ("timestamp", pa.int64()),
        ("helpful_vote", pa.int64()),
        ("verified_purchase", pa.bool_()),
    ]
)
META_SCHEMA = pa.schema(
    [
        ("main_category", pa.string()),
        ("title", pa.string()),
        ("average_rating", pa.float64()),
        ("rating_number", pa.int64()),
        ("features", pa.list_(pa.string())),
        ("description", pa.list_(pa.string())),
        ("price", pa.float64()),
        ("images", pa.string()),
        ("videos", pa.string()),
        ("store", pa.string()),
        ("categories", pa.list_(pa.string())),
        ("details", pa.string()),
        ("parent_asin", pa.string()),
        ("bought_together", pa.string()),
    ]
)
_JSON_FIELDS = {"images", "videos", "details", "bought_together"}


def categories() -> list[str]:
    """The 33 category names (plus 'Unknown') published with the dataset."""
    path = hf_hub_download(REPO_ID, "all_categories.txt", repo_type="dataset")
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def _price(v) -> float | None:
    """Metadata prices arrive as numbers, numeric strings, or 'None'."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize(row: dict, schema: pa.Schema) -> dict:
    out = {}
    for name in schema.names:
        v = row.get(name)
        if name in _JSON_FIELDS:
            v = json.dumps(v, ensure_ascii=False) if v not in (None, [], {}) else None
        elif name == "price":
            v = _price(v)
        out[name] = v
    return out


def _stream_jsonl(path: str) -> Iterator[dict]:
    fs = HfFileSystem()
    with fs.open(path, "rb", block_size=8 * 1024 * 1024) as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)


def _stream_to_parquet(
    src: str, out: Path, schema: pa.Schema, keep: Callable[[dict], bool], on_keep: Callable[[dict], None] | None = None
) -> int:
    tmp = out.with_suffix(".parquet.partial")
    n_seen = n_kept = 0
    batch: list[dict] = []
    with ParquetAppender(tmp, schema) as w:
        for row in _stream_jsonl(src):
            n_seen += 1
            if not keep(row):
                continue
            if on_keep:
                on_keep(row)
            batch.append(_normalize(row, schema))
            n_kept += 1
            if len(batch) >= BATCH:
                w.write(batch)
                batch.clear()
            if n_seen % 1_000_000 == 0:
                log.info("  %s rows read, %s kept", f"{n_seen:,}", f"{n_kept:,}")
        w.write(batch)
    tmp.replace(out)  # only a finished file gets the final name
    log.info("  %s: %s of %s rows kept", out.name, f"{n_kept:,}", f"{n_seen:,}")
    return n_kept


def download(settings: Settings, force: bool = False) -> None:
    cat = settings.category
    known = categories()
    if cat not in known:
        raise SystemExit(f"unknown McAuley category {cat!r}. Choose one of: {', '.join(known)}")
    lo, hi = settings.review_start_ms, settings.review_end_ms
    settings.reviews_path.parent.mkdir(parents=True, exist_ok=True)
    settings.meta_path.parent.mkdir(parents=True, exist_ok=True)

    parents: set[str] = set()
    if settings.reviews_path.exists() and not force:
        log.info("reviews already present: %s (use --force to redo)", settings.reviews_path)
        parents = set(pq.read_table(settings.reviews_path, columns=["parent_asin"])["parent_asin"].to_pylist())
    else:
        log.info("pass 1: %s reviews in [%d, %d] ms", cat, lo, hi)
        _stream_to_parquet(
            f"{HF_ROOT}/raw/review_categories/{cat}.jsonl",
            settings.reviews_path,
            REVIEW_SCHEMA,
            keep=lambda r: lo <= (r.get("timestamp") or 0) <= hi,
            on_keep=lambda r: parents.add(r.get("parent_asin")),
        )
    log.info("%s parent ASINs with a review in the window", f"{len(parents):,}")

    if settings.meta_path.exists() and not force:
        log.info("metadata already present: %s", settings.meta_path)
        return
    log.info("pass 2: %s item metadata", cat)
    _stream_to_parquet(
        f"{HF_ROOT}/raw/meta_categories/meta_{cat}.jsonl",
        settings.meta_path,
        META_SCHEMA,
        keep=lambda r: r.get("parent_asin") in parents,
    )
