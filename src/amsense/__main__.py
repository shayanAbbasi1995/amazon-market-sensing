"""Command line: ``python -m amsense <command> [options]``.

mcauley    download the configured McAuley category, filtered to review_window
collect    pull Keepa histories (reference marketplace first, then the rest)
universe   build the shared ASIN universe from the reference marketplace
panels     build monthly / weekly panels per marketplace
fx         download monthly FX rates (for cross-country price comparisons)
figures    draw the README figures from built panels
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from amsense import config


def _setup_logging(settings: config.Settings, command: str) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for h in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / f"{command}_{stamp}.log", encoding="utf-8"),
    ):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "huggingface_hub", "urllib3"):  # one line per HTTP request, with signed URLs
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _marketplaces(args: argparse.Namespace, settings: config.Settings) -> list[str]:
    return [m.lower() for m in args.marketplace] if args.marketplace else settings.collection_order


def cmd_mcauley(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import mcauley

    mcauley.download(settings, force=args.force)


def cmd_collect(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import collect
    from amsense.marketplaces import get

    for locale in _marketplaces(args, settings):
        if args.reprocess_raw:
            collect.reprocess_raw(settings.keepa_dir(locale), get(locale).price_divisor, settings.checkpoint_every)
            continue
        if args.asins:
            asins = collect._dedup(a.strip() for a in args.asins.split(","))
        elif args.asins_file:
            asins = collect.asins_from_file(Path(args.asins_file))
        elif args.asins_parquet:
            asins = collect.asins_from_parquet(Path(args.asins_parquet), args.asins_column)
        else:
            asins = collect.default_asins(settings, locale)
        asins = collect.select_asins(asins, args.limit, args.sample, args.seed)
        collect.collect(settings, locale, asins, resume=not args.no_resume)


def cmd_universe(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import universe

    universe.build(settings, force=args.force)


def cmd_panels(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import cleaning, panels
    from amsense.marketplaces import get

    for locale in _marketplaces(args, settings):
        if args.show_rules:
            print(cleaning.rules_for(get(locale), settings).summary())
            continue
        try:
            panels.build(settings, locale, args.panel)
        except FileNotFoundError as exc:  # marketplace not collected (yet)
            logging.warning("[%s] skipped: %s", locale, exc)


def cmd_fx(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import fx

    fx.fetch(settings, start=args.start)


def cmd_figures(args: argparse.Namespace, settings: config.Settings) -> None:
    from amsense import figures

    figures.make_all(settings, only=args.only)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="amsense", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="path to config.yaml (default: $AMSENSE_CONFIG or ./config.yaml)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mcauley", help="download and filter the McAuley category")
    p.add_argument("--force", action="store_true", help="redo even if output exists")
    p.set_defaults(func=cmd_mcauley)

    p = sub.add_parser("collect", help="collect Keepa histories")
    p.add_argument(
        "--marketplace", "-m", action="append", help="limit to this marketplace (repeatable); default: all in config"
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--asins", help="comma-separated ASINs (for testing)")
    src.add_argument("--asins-file", help="newline-delimited ASIN list")
    src.add_argument("--asins-parquet", help="parquet file with an ASIN column")
    p.add_argument("--asins-column", default="asin")
    p.add_argument("--limit", type=int, help="first N ASINs only")
    p.add_argument("--sample", type=int, help="random N ASINs (see --seed)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-resume", action="store_true", help="ignore the visited ledger")
    p.add_argument(
        "--reprocess-raw", action="store_true", help="rebuild parquet tables from the raw JSON archive (0 tokens)"
    )
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("universe", help="build the shared ASIN universe")
    p.add_argument("--force", action="store_true", help="overwrite an existing universe")
    p.set_defaults(func=cmd_universe)

    p = sub.add_parser("panels", help="build research panels")
    p.add_argument("--marketplace", "-m", action="append")
    p.add_argument(
        "--panel", action="append", help="monthly_obs, monthly_locf, weekly_obs, weekly_locf (default: panels.build)"
    )
    p.add_argument("--show-rules", action="store_true", help="print cleaning thresholds and exit")
    p.set_defaults(func=cmd_panels)

    p = sub.add_parser("fx", help="download monthly FX rates")
    p.add_argument("--start", default="2017-01-01")
    p.set_defaults(func=cmd_fx)

    p = sub.add_parser("figures", help="draw README figures")
    p.add_argument("--only", action="append", help="figure name(s) to draw")
    p.set_defaults(func=cmd_figures)

    args = ap.parse_args(argv)
    settings = config.load(args.config)
    _setup_logging(settings, args.command)
    args.func(args, settings)


if __name__ == "__main__":
    main()
