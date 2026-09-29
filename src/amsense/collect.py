"""Collect full Keepa histories for a list of ASINs in one or more marketplaces.

Durability model:
  * Every API response is archived to ``raw/*.json.gz`` *before* parsing, with
    the list of requested ASINs. The raw archive is the source of truth.
  * Parsed rows stream to part files under ``history/ daily/ snapshot/
    product/ coverage/``. A checkpoint closes the current parts and opens new
    ones; nothing is re-read, so memory stays flat on 300K+ ASIN runs.
  * ``visited_keepa.txt`` records every ASIN whose response was archived, so a
    rerun resumes where it stopped. ``--reprocess-raw`` rebuilds every parquet
    table from the raw archive for 0 tokens.

Which ASINs:
  * reference marketplace: the child ``asin`` values in the McAuley reviews
  * every other marketplace: the shared universe built by ``amsense universe``
    (so cross-country differences in coverage are findings, not filtering)
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from amsense import marketplaces as mkt
from amsense.config import Settings
from amsense.keepa_client import MAX_ASINS_PER_REQUEST, KeepaClient, KeepaError
from amsense.parse_keepa import (
    downsample_daily,
    not_found_coverage,
    parse_product,
    parse_product_attributes,
)
from amsense.schemas import (
    KEEPA_COVERAGE_SCHEMA,
    KEEPA_DAILY_SCHEMA,
    KEEPA_HISTORY_SCHEMA,
    KEEPA_PRODUCT_SCHEMA,
    KEEPA_SNAPSHOT_SCHEMA,
)
from amsense.storage import ParquetAppender

log = logging.getLogger(__name__)

TABLES = {
    "history": KEEPA_HISTORY_SCHEMA,
    "daily": KEEPA_DAILY_SCHEMA,
    "snapshot": KEEPA_SNAPSHOT_SCHEMA,
    "product": KEEPA_PRODUCT_SCHEMA,
    "coverage": KEEPA_COVERAGE_SCHEMA,
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ── ASIN sources ─────────────────────────────────────────────────────────────
def _dedup(values) -> list[str]:
    """Drop empties and duplicates, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for a in values:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def asins_from_parquet(path: Path, column: str = "asin") -> list[str]:
    if not path.exists():
        raise SystemExit(f"ASIN parquet not found: {path}")
    asins = _dedup(pq.read_table(path, columns=[column])[column].to_pylist())
    log.info("%d unique ASINs from %s", len(asins), path)
    return asins


def asins_from_file(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"ASIN file not found: {path}")
    asins = _dedup(line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    log.info("%d unique ASINs from %s", len(asins), path)
    return asins


def default_asins(settings: Settings, locale: str) -> list[str]:
    """McAuley child ASINs for the reference marketplace, the universe elsewhere."""
    if locale == settings.reference_marketplace:
        if not settings.reviews_path.exists():
            raise SystemExit(
                f"McAuley reviews not found at {settings.reviews_path}. Run `python -m amsense mcauley` first."
            )
        return asins_from_parquet(settings.reviews_path)
    if not settings.universe_path.exists():
        raise SystemExit(
            f"{locale}: the shared ASIN universe {settings.universe_path} does not exist yet. Collect "
            f"the reference marketplace ({settings.reference_marketplace}) and run `python -m amsense universe`."
        )
    return asins_from_parquet(settings.universe_path)


# ── Output ───────────────────────────────────────────────────────────────────
class RunWriters:
    """One open part file per table; ``checkpoint()`` finalizes and rotates them."""

    def __init__(self, out_dir: Path, run_id: str):
        self.out_dir = out_dir
        self.run_id = run_id
        self.seq = 0
        for name in TABLES:
            (out_dir / name).mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self.appenders = {
            name: ParquetAppender(self.out_dir / name / f"part_{self.run_id}_{self.seq:05d}.parquet", schema)
            for name, schema in TABLES.items()
        }

    def write(self, rows: dict[str, list[dict]]) -> None:
        for name, appender in self.appenders.items():
            appender.write(rows[name])

    def checkpoint(self) -> None:
        self.close()
        self.seq += 1
        self._open()

    def close(self) -> None:
        for a in self.appenders.values():
            a.close()


def save_raw(raw_dir: Path, run_id: str, seq: int, requested: list[str], response: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "seq": seq,
        "fetched_at_ms": int(time.time() * 1000),
        "requested_asins": requested,
        "response": response,
    }
    with gzip.open(raw_dir / f"resp_{run_id}_{seq:06d}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def rows_from_response(
    requested: list[str], response: dict, run_id: str, crawl_ts: int, price_divisor: float
) -> dict[str, list[dict]]:
    """Parse one response into rows for every table. Every requested ASIN gets a
    coverage row, including ones Keepa did not return (status ``not_found``)."""
    rows: dict[str, list[dict]] = {name: [] for name in TABLES}
    found: set[str] = set()
    for product in response.get("products") or []:
        asin = product.get("asin")
        if not asin:
            continue
        found.add(asin)
        hist, snap, cov = parse_product(product, run_id, crawl_ts, price_divisor)
        rows["history"].extend(hist)
        if snap is not None:
            rows["snapshot"].append(snap)
        rows["product"].append(parse_product_attributes(product, run_id, crawl_ts))
        rows["coverage"].append(cov)
    for asin in requested:
        if asin not in found:
            rows["coverage"].append(not_found_coverage(asin, run_id, crawl_ts))
    rows["daily"] = downsample_daily(rows["history"])
    return rows


# ── Commands ─────────────────────────────────────────────────────────────────
def reprocess_raw(out_dir: Path, price_divisor: float, checkpoint_every: int) -> None:
    """Rebuild every parquet table from ``raw/*.json.gz``. Costs 0 tokens."""
    files = sorted((out_dir / "raw").glob("*.json.gz"))
    if not files:
        raise SystemExit(f"no raw files in {out_dir / 'raw'}")
    log.info("reprocessing %d raw files in %s (0 tokens)", len(files), out_dir)
    for name in TABLES:
        for p in (out_dir / name).glob("*.parquet"):
            p.unlink()
    run_id = "reproc_" + _utc_stamp()
    writers = RunWriters(out_dir, run_id)
    for i, fp in enumerate(files, 1):
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        writers.write(
            rows_from_response(
                payload.get("requested_asins", []),
                payload.get("response", {}),
                run_id,
                payload.get("fetched_at_ms", int(time.time() * 1000)),
                price_divisor,
            )
        )
        if i % checkpoint_every == 0:
            writers.checkpoint()
            log.info("  %d/%d raw files", i, len(files))
    writers.close()
    log.info("reprocess complete: %s", out_dir)


def collect(settings: Settings, locale: str, asins: list[str], resume: bool = True) -> None:
    """Fetch ``asins`` from one marketplace, 100 per request, pacing on tokens."""
    m = mkt.get(locale)
    out_dir = settings.keepa_dir(locale)
    out_dir.mkdir(parents=True, exist_ok=True)
    visited_path = out_dir / "visited_keepa.txt"

    visited: set[str] = set()
    if resume and visited_path.exists():
        visited = {ln.strip() for ln in visited_path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    todo = [a for a in asins if a not in visited]
    run_id = _utc_stamp()
    log.info(
        "[%s] run %s: %d ASINs, %d already visited, %d to fetch -> %s",
        m.code,
        run_id,
        len(asins),
        len(asins) - len(todo),
        len(todo),
        out_dir,
    )
    if not todo:
        log.info("[%s] nothing to do", m.code)
        return

    client = KeepaClient(settings.keepa_api_key, domain=m.domain)
    writers = RunWriters(out_dir, run_id)
    n_found = 0
    started = time.time()
    seq = 0
    with open(visited_path, "a", encoding="utf-8") as visited_f:
        for start in range(0, len(todo), MAX_ASINS_PER_REQUEST):
            chunk = todo[start : start + MAX_ASINS_PER_REQUEST]
            seq += 1
            crawl_ts = int(time.time() * 1000)
            client.wait_for_tokens(2 * len(chunk))  # worst case ~2 tokens per ASIN
            try:
                response = client.fetch(chunk, stats=settings.keepa_stats)
            except KeepaError as exc:
                # Network trouble, 429 and 5xx are retried inside fetch(). What reaches
                # here (bad key, no active plan, invalid parameter) would fail on every
                # batch, so stop. Unfetched ASINs are not in the ledger; a rerun resumes.
                writers.close()
                raise SystemExit(
                    f"[{m.code}] Keepa rejected the request: {exc}\nCheck KEEPA_API_KEY and your Keepa plan."
                ) from None

            save_raw(out_dir / "raw", run_id, seq, chunk, response)  # 1. archive
            rows = rows_from_response(chunk, response, run_id, crawl_ts, m.price_divisor)
            writers.write(rows)  # 2. parse
            visited_f.writelines(a + "\n" for a in chunk)  # 3. ledger
            visited_f.flush()

            batch_found = sum(1 for c in rows["coverage"] if c["found_in_keepa"])
            n_found += batch_found
            done = start + len(chunk)
            rate = done / max(time.time() - started, 1e-6)
            log.info(
                "[%s] batch %d | found %d/%d | tokensLeft %s | consumed %d | %.1f ASIN/s | ETA %.0f min",
                m.code,
                seq,
                batch_found,
                len(chunk),
                client.tokens_left,
                client.total_tokens_consumed,
                rate,
                (len(todo) - done) / rate / 60,
            )
            if seq % settings.checkpoint_every == 0:
                writers.checkpoint()
    writers.close()

    log.info(
        "[%s] done in %.1f min: %d requested, %d found, %d tokens",
        m.code,
        (time.time() - started) / 60,
        len(todo),
        n_found,
        client.total_tokens_consumed,
    )


def select_asins(asins: list[str], limit: int | None, sample: int | None, seed: int) -> list[str]:
    """Apply --sample (reproducible random subset) or --limit (first N)."""
    if sample and sample < len(asins):
        log.info("random sample of %d ASINs (seed=%d)", sample, seed)
        return random.Random(seed).sample(asins, sample)
    if limit:
        return asins[:limit]
    return asins
