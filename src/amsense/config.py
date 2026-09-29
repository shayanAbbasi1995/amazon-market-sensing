"""Load ``config.yaml`` (what to collect, where to put it) and ``.env`` (secrets).

Every other module takes a :class:`Settings` object instead of reading paths or
environment variables itself, so pointing the pipeline at a new category, a new
set of marketplaces or a different disk is a config edit only.

Path templates in ``paths:`` may use ``{data}``, ``{category}``, ``{CODE}``
(uppercase marketplace code) and ``{code}`` (lowercase). Relative paths are
resolved against the directory holding the config file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from amsense import marketplaces as mkt

DEFAULT_PATHS: dict[str, str] = {
    "data": "./data",
    "reviews": "{data}/{category}/mcauley/reviews.parquet",
    "metadata": "{data}/{category}/mcauley/metadata.parquet",
    "universe": "{data}/{category}/universe.parquet",
    "keepa": "{data}/{category}/keepa/{CODE}",
    "panels": "{data}/{category}/panels/{CODE}",
    "fx": "{data}/fx_monthly.parquet",
    "tmp": "{data}/duckdb_tmp",
    "figures": "./figures",
}

DEFAULT_CLEANING: dict[str, float] = {
    "price_ceiling_usd": 10_000.0,
    "penny_threshold_usd": 1.0,
    "review_count_ceiling": 10_000_000,
    "min_date": "2011-01-01",
}


def date_to_ms(value: Any, end_of_day: bool = False) -> int:
    """'2023-01-01' (or a date object) -> Unix ms at 00:00:00 UTC (or 23:59:59.999)."""
    d = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    ms = int(d.timestamp() * 1000)
    return ms + 86_400_000 - 1 if end_of_day else ms


@dataclass(frozen=True)
class Settings:
    category: str
    review_start_ms: int
    review_end_ms: int
    marketplaces: tuple[str, ...]
    reference_marketplace: str
    price_quantile_cut: float
    checkpoint_every: int
    keepa_stats: str | None
    cleaning: dict[str, Any]
    panels: tuple[str, ...]
    panel_batches: int
    duckdb_memory_limit: str
    duckdb_threads: int
    paths: dict[str, str]
    base_dir: Path
    source_file: Path | None = field(default=None, compare=False)

    # -- secrets --------------------------------------------------------------
    @property
    def keepa_api_key(self) -> str:
        """Keepa key from the environment (``.env`` is loaded by :func:`load`)."""
        key = os.environ.get("KEEPA_API_KEY", "").strip().strip("<>\"'").strip()
        if not key:
            raise SystemExit(
                "KEEPA_API_KEY is not set. Copy .env.example to .env and paste your key, "
                "or export KEEPA_API_KEY in your shell."
            )
        return key

    # -- paths ----------------------------------------------------------------
    def _resolve(self, name: str, code: str | None = None) -> Path:
        tmpl = self.paths[name]
        fmt = {"category": self.category, "data": self.paths["data"]}
        if code is not None:
            fmt.update(CODE=code.upper(), code=code.lower())
        p = Path(tmpl.format(**fmt)).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def data_dir(self) -> Path:
        return self._resolve("data")

    @property
    def reviews_path(self) -> Path:
        return self._resolve("reviews")

    @property
    def meta_path(self) -> Path:
        return self._resolve("metadata")

    @property
    def universe_path(self) -> Path:
        return self._resolve("universe")

    @property
    def fx_path(self) -> Path:
        return self._resolve("fx")

    @property
    def tmp_dir(self) -> Path:
        return self._resolve("tmp")

    @property
    def figures_dir(self) -> Path:
        return self._resolve("figures")

    def keepa_dir(self, locale: str) -> Path:
        return self._resolve("keepa", locale)

    def panels_dir(self, locale: str) -> Path:
        return self._resolve("panels", locale)

    # -- derived --------------------------------------------------------------
    @property
    def collection_order(self) -> list[str]:
        """Reference marketplace first, then the rest in config order."""
        ref = self.reference_marketplace
        return [ref] + [m for m in self.marketplaces if m != ref]


def load(path: str | Path | None = None) -> Settings:
    """Read the YAML config and the ``.env`` next to it.

    Lookup order when ``path`` is None: ``$AMSENSE_CONFIG``, ``./config.yaml``.
    Shell environment variables win over values in ``.env``.
    """
    path = Path(path or os.environ.get("AMSENSE_CONFIG") or "config.yaml")
    if not path.exists():
        raise SystemExit(
            f"config file not found: {path}. Copy config.example.yaml to config.yaml and edit it, or pass --config."
        )
    base = path.resolve().parent
    load_dotenv(base / ".env", override=False)
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if "category" not in raw:
        raise SystemExit(f"{path}: 'category' is required (a McAuley 2023 category name)")

    window = raw.get("review_window") or {}
    locales = tuple(m.lower() for m in raw.get("marketplaces", ["us"]))
    for m in locales:
        mkt.get(m)  # fail early on a typo
    ref = str(raw.get("reference_marketplace", locales[0])).lower()
    if ref not in locales:
        raise SystemExit(f"reference_marketplace {ref!r} is not in marketplaces {locales}")

    keepa = raw.get("keepa") or {}
    panels = raw.get("panels") or {}
    duck = raw.get("duckdb") or {}
    stats = keepa.get("stats")

    return Settings(
        category=str(raw["category"]),
        review_start_ms=date_to_ms(window.get("start", "2023-01-01")),
        review_end_ms=date_to_ms(window.get("end", "2023-12-31"), end_of_day=True),
        marketplaces=locales,
        reference_marketplace=ref,
        price_quantile_cut=float((raw.get("universe") or {}).get("price_quantile_cut", 0.99)),
        checkpoint_every=int(keepa.get("checkpoint_every", 25)),
        keepa_stats=None if stats is None else str(stats),
        cleaning={**DEFAULT_CLEANING, **(raw.get("cleaning") or {})},
        panels=tuple(panels.get("build", ["monthly_obs", "monthly_locf", "weekly_locf"])),
        panel_batches=int(panels.get("batches", 16)),
        duckdb_memory_limit=str(duck.get("memory_limit", "8GB")),
        duckdb_threads=int(duck.get("threads", 4)),
        paths={**DEFAULT_PATHS, **(raw.get("paths") or {})},
        base_dir=base,
        source_file=path,
    )
