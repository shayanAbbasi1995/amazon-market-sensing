"""Thin, direct client for the official Keepa Product REST API.

No third-party wrapper library — just ``requests`` against
``https://api.keepa.com/product``, plus our own token accounting so the
collector can pace itself against the subscription's tokens-per-minute limit.

Token model (from Keepa docs): each response carries ``tokensLeft``,
``refillIn`` (ms until the next refill tick), ``refillRate`` (tokens/min) and
``tokensConsumed``. We keep enough state to sleep before a call that would
overdraw the bucket.
"""

from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.keepa.com"
MAX_ASINS_PER_REQUEST = 100

# Retry pacing for *transient* failures — a lost/errored connection or a Keepa
# 429/5xx. These are retried indefinitely with capped exponential backoff so an
# internet (or Keepa) outage PAUSES the crawl instead of skipping ASINs; the
# collector resumes on its own when the connection returns. Only a genuinely
# non-retryable HTTP error (a non-429 4xx, e.g. a bad API key) raises.
RETRY_BACKOFF_START = 5.0  # seconds, first wait
RETRY_BACKOFF_CAP = 300.0  # seconds, ceiling (re-checks ~every 5 min in a long outage)


class KeepaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class KeepaClient:
    def __init__(self, key: str, domain: int = 1, session: requests.Session | None = None):
        # The key comes from Settings.keepa_api_key (.env or shell env).
        self.key = key.strip()
        if not self.key:
            raise KeepaError("empty Keepa API key")
        self.domain = domain
        self.session = session or requests.Session()
        # token state (populated after the first response)
        self.tokens_left: int | None = None
        self.refill_rate: int | None = None  # tokens per minute
        self.refill_in: int | None = None  # ms until next refill
        self.total_tokens_consumed = 0

    # -- token pacing ------------------------------------------------------
    def wait_for_tokens(self, needed: int) -> None:
        """Block until we expect at least ``needed`` tokens to be available.

        On the first call token state is unknown, so we just proceed and let
        the response tell us where we stand.
        """
        if self.tokens_left is None or self.tokens_left >= needed:
            return
        deficit = needed - self.tokens_left
        # seconds to accrue the deficit at the refill rate, plus the current
        # partial-tick wait; fall back to 60s if the rate is unknown.
        if self.refill_rate and self.refill_rate > 0:
            wait_s = (deficit / self.refill_rate) * 60.0
        else:
            wait_s = 60.0
        if self.refill_in:
            wait_s = max(wait_s, self.refill_in / 1000.0)
        wait_s = min(wait_s, 3600.0)  # safety cap
        log.info("Tokens low (%s left, need %s) — sleeping %.0fs to refill", self.tokens_left, needed, wait_s)
        time.sleep(wait_s)
        # assume the sleep restored roughly the deficit
        if self.tokens_left is not None:
            self.tokens_left += deficit

    # -- requests ----------------------------------------------------------
    def fetch(self, asins: list[str], stats: str | int | None = None, timeout: int = 90) -> dict:
        """Fetch up to 100 ASINs in one request. Returns the parsed JSON dict.

        Params are fixed to our collection policy: full history, rating history,
        and ``update=-1`` (never trigger a paid live refresh; unknown ASINs cost
        0 tokens).

        Resilience: transient failures — a lost/errored connection or a Keepa
        429/5xx — are retried *indefinitely* with capped exponential backoff, so
        an internet (or Keepa) outage pauses the crawl and it resumes on its own
        when the connection returns. No ASIN is ever skipped as a result of an
        outage. Only a non-retryable HTTP error (a non-429 4xx, e.g. a bad key)
        raises ``KeepaError``.
        """
        if not asins:
            return {"products": []}
        if len(asins) > MAX_ASINS_PER_REQUEST:
            raise KeepaError(f"max {MAX_ASINS_PER_REQUEST} ASINs per request")

        params = {"asin": ",".join(asins), "history": 1, "rating": 1, "update": -1}
        if stats is not None:
            params["stats"] = stats
        return self._get("product", params, timeout)

    def query(self, selection: dict, timeout: int = 90) -> dict:
        """Product Finder: ASINs matching a JSON selection (``asinList``, ``totalResults``).

        Build the selection on keepa.com's Product Finder and copy it from
        "Show API query". Pagination is ``page`` / ``perPage`` inside it.
        """
        return self._get("query", {"selection": json.dumps(selection)}, timeout)

    def bestsellers(self, category: str | int, timeout: int = 90) -> list[str]:
        """Best-seller ASINs of one Amazon category node, best-selling first."""
        data = self._get("bestsellers", {"category": str(category)}, timeout)
        return list((data.get("bestSellersList") or {}).get("asinList") or [])

    def _get(self, path: str, params: dict, timeout: int) -> dict:
        """GET one Keepa endpoint with the retry policy described in :meth:`fetch`."""
        params = {"key": self.key, "domain": self.domain, **params}
        url = f"{API_ROOT}/{path}"
        backoff = RETRY_BACKOFF_START
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                # Connection down / DNS failure / timeout — the internet is out.
                # Wait it out and retry forever; never give up on this batch.
                log.warning(
                    "Network error reaching Keepa (attempt %d): %s — waiting %.0fs "
                    "then retrying; crawl paused, no ASINs skipped.",
                    attempt,
                    self._redact(str(exc)),
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, RETRY_BACKOFF_CAP)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._update_tokens(data)
                return data

            # 429 = out of tokens / too many requests; 5xx = transient server
            # trouble. Both are outages from our side — wait and retry forever.
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = backoff
                try:  # honor refillIn if the body carries it
                    body = resp.json()
                    if body.get("refillIn"):
                        wait = max(wait, body["refillIn"] / 1000.0)
                except Exception:
                    pass
                log.warning(
                    "HTTP %d from Keepa (attempt %d) — waiting %.0fs then retrying; crawl paused, no ASINs skipped.",
                    resp.status_code,
                    attempt,
                    wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, RETRY_BACKOFF_CAP)
                continue

            # Other 4xx: unrecoverable (bad key, malformed request). Surface it —
            # this is not an outage and retrying will not fix it.
            raise KeepaError(self._redact(f"HTTP {resp.status_code}: {resp.text[:300]}"), resp.status_code)

    def _redact(self, text: str) -> str:
        """Request URLs carry ``?key=...``; never let the key reach a log file."""
        return text.replace(self.key, "***")

    def _update_tokens(self, data: dict) -> None:
        if "tokensLeft" in data:
            self.tokens_left = data["tokensLeft"]
        if "refillRate" in data:
            self.refill_rate = data["refillRate"]
        if "refillIn" in data:
            self.refill_in = data["refillIn"]
        consumed = data.get("tokensConsumed", 0) or 0
        self.total_tokens_consumed += consumed
