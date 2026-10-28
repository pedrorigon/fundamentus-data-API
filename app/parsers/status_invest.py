from __future__ import annotations

import unicodedata

from selectolax.parser import HTMLParser


def status_invest_cnpj(tree: HTMLParser) -> str | None:
    """Extract the registered fund CNPJ from a Status Invest profile."""
    for node in tree.css("h3.title, strong"):
        if _fold(node.text()) != "CNPJ":
            continue
        container = node.parent
        value_node = (
            container.css_first("strong.value, .span-item") if container is not None else None
        )
        digits = (
            "".join(character for character in value_node.text() if character.isdigit())
            if value_node
            else ""
        )
        if len(digits) == 14:
            return digits
    return None


def parse_status_invest_cnpj(html: str) -> str | None:
    return status_invest_cnpj(HTMLParser(html))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return (
        "".join(character for character in normalized if not unicodedata.combining(character))
        .strip()
        .upper()
    )


__all__ = ["parse_status_invest_cnpj", "status_invest_cnpj"]
