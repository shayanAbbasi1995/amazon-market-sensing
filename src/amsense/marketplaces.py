"""Amazon marketplaces known to Keepa: locale code <-> Keepa domainId <-> currency.

Keepa addresses each Amazon storefront by an integer ``domain``. The same ASIN
can be queried against any of them; an ASIN that is not listed in a marketplace
comes back as ``not_found`` and costs 0 tokens (we always send ``update=-1``).

The list below is every locale in Keepa's official API client
(keepacom/api_backend, AmazonLocale.java). Domain 7 (amazon.cn) is reserved
and no longer served.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Marketplace:
    locale: str  # lowercase code used in config and on the command line
    domain: int  # Keepa domainId
    currency: str  # ISO 4217 code of prices in this marketplace
    country: str  # human-readable name for logs and figures

    @property
    def code(self) -> str:
        """Uppercase code, stored in the panels' ``marketplace`` column."""
        return self.locale.upper()

    @property
    def price_divisor(self) -> float:
        """Keepa stores prices as integers in "the respective Amazon locale's
        smallest currency unit (e.g. euro cents or yen)" (api_backend Product.java)."""
        return 1.0 if self.currency == "JPY" else 100.0


MARKETPLACES: dict[str, Marketplace] = {
    m.locale: m
    for m in (
        Marketplace("us", 1, "USD", "United States"),
        Marketplace("uk", 2, "GBP", "United Kingdom"),
        Marketplace("de", 3, "EUR", "Germany"),
        Marketplace("fr", 4, "EUR", "France"),
        Marketplace("jp", 5, "JPY", "Japan"),
        Marketplace("ca", 6, "CAD", "Canada"),
        Marketplace("it", 8, "EUR", "Italy"),
        Marketplace("es", 9, "EUR", "Spain"),
        Marketplace("in", 10, "INR", "India"),
        Marketplace("mx", 11, "MXN", "Mexico"),
        Marketplace("br", 12, "BRL", "Brazil"),
    )
}


def get(locale: str) -> Marketplace:
    """Look up a marketplace by locale code, case-insensitively."""
    key = locale.strip().lower()
    if key not in MARKETPLACES:
        raise SystemExit(f"unknown marketplace {locale!r}. Known: {', '.join(MARKETPLACES)}")
    return MARKETPLACES[key]
