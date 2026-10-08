"""
Saarthi's own page addresses, for both products.

The two-wheeler and private-car journeys use the same page names, each under
its own prefix (Saarthi two-wheeler-routing.module.ts, private-car-routing
.module.ts):

    /two-wheeler/result/:quoteno        /private-car/result/:quoteno
    /two-wheeler/kyc-insurance          /private-car/kyc-insurance
    /two-wheeler/proposal               /private-car/proposal
    /two-wheeler/proposal-payment       /private-car/proposal-payment

so a page object asks "is this the proposal page?" through on() and works for
either. It is a prefix match, exactly like the old `"/two-wheeler/proposal" in
url` checks: "proposal" also matches "proposal-payment".
"""
from __future__ import annotations

PREFIXES = ("/two-wheeler/", "/private-car/")


def on(url: str, page: str) -> bool:
    """Is `url` our app's `page` page (e.g. "proposal"), for either product?"""
    low = (url or "").lower()
    return any(prefix + page in low for prefix in PREFIXES)


def product_of(url: str) -> str:
    """Which product's journey this address belongs to: 'car' or 'bike'."""
    return "car" if "/private-car/" in (url or "").lower() else "bike"
