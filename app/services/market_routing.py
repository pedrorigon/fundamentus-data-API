"""Decide which market a ticker belongs to before any provider is called.

The opportunity pipeline queried B3's daily bulletin for every ticker, walking
back up to seven trading sessions until one answered. For a Brazilian code that
is a reasonable way to skip holidays; for ``NVDA`` it is seven sequential HTTP
requests that can only ever return nothing, and it dominated the latency of
including a foreign asset.

Routing is therefore decided from the ticker's shape first. B3 codes are four
letters followed by a share-class digit (optionally with an ``F`` for the
odd-lot book), and Brazilian fund and BDR codes are equally regular. Anything
that cannot be a B3 code is not sent to B3 at all.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Market(StrEnum):
    """The venue family that owns an instrument's reference data."""

    B3 = "b3"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


#: ``PETR4``, ``BBAS3``, ``MXRF11``. The trailing ``F`` is the odd-lot code for
#: the same share, so it routes identically.
_B3_EQUITY = re.compile(r"^[A-Z]{4}(?:3|4|5|6|7|8|11)F?$")
#: ``NVDC34``, ``AAPL34`` and the rest of the 30-39 depositary receipt range.
_B3_BDR = re.compile(r"^[A-Z]{4}3[0-9]$")
#: Fractional and special-situation suffixes B3 appends to an otherwise valid
#: code (rights, receipts): still B3 instruments.
_B3_SPECIAL = re.compile(r"^[A-Z]{4}(?:1|2|9|10|12|13)$")

_FOREIGN_SUFFIXES = (".SA",)


def normalize_for_routing(ticker: str) -> str:
    return ticker.strip().upper()


def is_b3_ticker(ticker: str) -> bool:
    """Whether a code can be a B3 instrument at all."""
    normalized = normalize_for_routing(ticker)
    if not normalized or normalized.endswith(_FOREIGN_SUFFIXES):
        return False
    return bool(
        _B3_EQUITY.match(normalized) or _B3_BDR.match(normalized) or _B3_SPECIAL.match(normalized)
    )


def resolve_market(ticker: str) -> Market:
    """Market that owns ``ticker``'s reference data.

    A code that cannot be a B3 instrument is reported as foreign rather than
    unknown: the useful outcome is that the B3 pipeline is skipped, and the
    foreign providers already fail closed for a symbol they do not recognise.
    """
    normalized = normalize_for_routing(ticker)
    if not normalized:
        return Market.UNKNOWN
    return Market.B3 if is_b3_ticker(normalized) else Market.FOREIGN


def should_query_b3(ticker: str) -> bool:
    """Whether B3's bulletin is worth querying for ``ticker``."""
    return resolve_market(ticker) is Market.B3
