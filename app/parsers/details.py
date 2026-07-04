from datetime import UTC, datetime
from datetime import date as Date
from decimal import Decimal
from typing import cast

from selectolax.parser import HTMLParser, Node

from app.core.errors import AssetNotFoundError, UpstreamInvalidResponseError
from app.models import AssetDetails, DetailSection, FieldData
from app.models.assets import JsonValue
from app.parsers.normalizers import clean_text, infer_value, normalize_key


def _cell_text(cell: Node) -> str:
    txt = cell.css_first("span.txt")
    return clean_text(txt.text(separator=" ") if txt else cell.text(separator=" "))


def _class_names(cell: Node) -> set[str]:
    return set((cell.attributes.get("class") or "").split())


def _header_name(
    primary_by_col: dict[int, str],
    secondary_by_col: dict[int, str],
    col_index: int,
    fallback: str,
) -> str:
    secondary = secondary_by_col.get(col_index)
    primary = primary_by_col.get(col_index)
    if primary and secondary:
        return f"{primary} - {secondary}"
    return secondary or primary or fallback


def _append_field(
    sections_by_name: dict[str, DetailSection],
    section_name: str,
    label: str,
    raw_value: str,
) -> None:
    if not label:
        return
    value, value_type = infer_value(raw_value, label)
    section = sections_by_name.setdefault(
        section_name,
        DetailSection(name=section_name, key_normalized=normalize_key(section_name), fields=[]),
    )
    section.fields.append(
        FieldData(
            label=label,
            key_normalized=normalize_key(label),
            value=cast(JsonValue, value),
            raw_value=clean_text(raw_value) or None,
            value_type=value_type,
        )
    )


def _extract_sections(tree: HTMLParser) -> list[DetailSection]:
    sections_by_name: dict[str, DetailSection] = {}
    tables = tree.css("table.w728")
    fallback_names = ["Identity", "Market", "Indicators"]

    for table_index, table in enumerate(tables):
        fallback = fallback_names[table_index] if table_index < len(fallback_names) else "Detalhes"
        primary_by_col: dict[int, str] = {}
        secondary_by_col: dict[int, str] = {}

        for row in table.css("tr"):
            cells = row.css("td")
            if not cells:
                continue

            col = 0
            for cell in cells:
                classes = _class_names(cell)
                colspan = int(cell.attributes.get("colspan") or "1")
                text = _cell_text(cell)
                if "nivel1" in classes:
                    for idx in range(col, col + colspan):
                        primary_by_col[idx] = text
                        secondary_by_col.pop(idx, None)
                elif "nivel2" in classes:
                    for idx in range(col, col + colspan):
                        secondary_by_col[idx] = text
                col += colspan

            col = 0
            indexed_cells: list[tuple[int, Node]] = []
            for cell in cells:
                indexed_cells.append((col, cell))
                col += int(cell.attributes.get("colspan") or "1")

            for idx, (col_index, cell) in enumerate(indexed_cells):
                if "label" not in _class_names(cell):
                    continue
                label = _cell_text(cell)
                value_cell: Node | None = None
                for _next_col, candidate in indexed_cells[idx + 1 :]:
                    if "data" in _class_names(candidate):
                        value_cell = candidate
                        break
                    if "label" in _class_names(candidate):
                        break
                if value_cell is None:
                    continue
                section_name = _header_name(primary_by_col, secondary_by_col, col_index, fallback)
                _append_field(sections_by_name, section_name, label, _cell_text(value_cell))

    return list(sections_by_name.values())


def _find_field(sections: list[DetailSection], *labels: str) -> object | None:
    keys = {normalize_key(label) for label in labels}
    for section in sections:
        for field in section.fields:
            if field.key_normalized in keys:
                return field.value
    return None


def _find_text(sections: list[DetailSection], *labels: str) -> str | None:
    value = _find_field(sections, *labels)
    return value if isinstance(value, str) else None


def _find_decimal(sections: list[DetailSection], *labels: str) -> Decimal | None:
    value = _find_field(sections, *labels)
    return value if isinstance(value, Decimal) else None


def _find_date(sections: list[DetailSection], *labels: str) -> Date | None:
    value = _find_field(sections, *labels)
    return value if isinstance(value, Date) else None


def parse_asset_details(html: str, ticker: str, source_url: str) -> AssetDetails:
    if not html or "detalhes" not in html.lower():
        raise UpstreamInvalidResponseError(ticker=ticker)

    tree = HTMLParser(html)
    sections = _extract_sections(tree)
    parsed_ticker = _find_text(sections, "Papel")
    if not parsed_ticker:
        if "papel não encontrado" in html.lower() or "papel nao encontrado" in html.lower():
            raise AssetNotFoundError(ticker=ticker)
        raise UpstreamInvalidResponseError(ticker=ticker)

    normalized_ticker = clean_text(parsed_ticker).upper()
    if normalized_ticker != ticker.upper():
        raise AssetNotFoundError(ticker=ticker)

    return AssetDetails(
        ticker=normalized_ticker,
        company_name=_find_text(sections, "Empresa"),
        asset_type=_find_text(sections, "Tipo"),
        quote=_find_decimal(sections, "Cotação"),
        quote_date=_find_date(sections, "Data últ cot"),
        market_value=_find_decimal(sections, "Valor de mercado"),
        enterprise_value=_find_decimal(sections, "Valor da firma"),
        shares_count=_find_decimal(sections, "Nro. Ações"),
        last_balance_date=_find_date(sections, "Últ balanço processado"),
        sector=_find_text(sections, "Setor"),
        subsector=_find_text(sections, "Subsetor"),
        average_daily_volume_2m=_find_decimal(sections, "Vol $ méd (2m)"),
        book_value_per_share=_find_decimal(sections, "VPA"),
        earnings_per_share=_find_decimal(sections, "LPA"),
        min_52_weeks=_find_decimal(sections, "Min 52 sem"),
        max_52_weeks=_find_decimal(sections, "Max 52 sem"),
        sections=sections,
        source_url=source_url,
        scraped_at=datetime.now(UTC),
    )
