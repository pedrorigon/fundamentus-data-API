from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from selectolax.parser import HTMLParser

from app.income.resolver import canonical_event_type
from app.models.income_events import IncomeEventObservation


def parse_b3_income_events(
    payload: Any,
    *,
    ticker: str,
    requested_isin: str | None = None,
) -> tuple[list[IncomeEventObservation], str | None]:
    rows = payload if isinstance(payload, list) else []
    company = next((row for row in rows if isinstance(row, dict)), None)
    if company is None:
        return [], None
    cvm_code = _text(company.get("codeCVM"))
    observations: list[IncomeEventObservation] = []
    seen: set[str] = set()
    for row in company.get("cashDividends") or []:
        if not isinstance(row, dict):
            continue
        isin = _text(row.get("isinCode") or row.get("assetIssued"))
        if requested_isin and isin and requested_isin.upper() != isin.upper():
            continue
        ex_date = _br_date(row.get("lastDatePrior"))
        payment_date = _br_date(row.get("paymentDate"))
        amount = _decimal(row.get("rate"))
        if ex_date is None or payment_date is None or amount is None or amount <= 0:
            continue
        source_id = _digest(
            "b3",
            isin or ticker,
            canonical_event_type(_text(row.get("label")) or "Provento"),
            ex_date.isoformat(),
            str(amount),
        )
        if source_id in seen:
            continue
        seen.add(source_id)
        observations.append(
            IncomeEventObservation(
                source="b3",
                lineage="official:b3",
                source_event_id=source_id,
                ticker=ticker,
                isin=isin,
                event_type=_text(row.get("label")) or "Provento",
                ex_date=ex_date,
                payment_date=payment_date,
                unit_price=amount,
                reference_period=_text(row.get("relatedTo")),
                authority=90,
                payload_hash=_payload_hash(row),
            )
        )
    return observations, cvm_code


def parse_fundos_net_xml(
    content: bytes,
    *,
    document_id: str,
    version: int = 1,
    source_status: str = "active",
) -> list[IncomeEventObservation]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    observations: list[IncomeEventObservation] = []
    for ordinal, provento in enumerate(root.findall(".//Provento")):
        isin = _xml_text(provento, "CodISIN")
        ticker = (_xml_text(provento, "CodNegociacao") or "").upper()
        if not ticker:
            continue
        for event_tag, event_node in _fnet_event_nodes(provento):
            ex_date = _iso_date(_xml_text(event_node, "DataBase"))
            payment_date = _iso_date(_xml_text(event_node, "DataPagamento"))
            amount = _decimal(_xml_text(event_node, "ValorProvento"))
            if ex_date is None or payment_date is None or amount is None or amount <= 0:
                continue
            source_id = f"fnet:{document_id}:{ordinal}:{event_tag.lower()}"
            observations.append(
                IncomeEventObservation(
                    source="fundos_net",
                    lineage="official:fundos_net",
                    source_event_id=source_id,
                    ticker=ticker,
                    isin=isin,
                    event_type=event_tag,
                    ex_date=ex_date,
                    payment_date=payment_date,
                    unit_price=amount,
                    reference_period=_xml_text(event_node, "PeriodoReferencia"),
                    source_status=source_status,
                    source_version=max(version, 1),
                    authority=100,
                    payload_hash=hashlib.sha256(content).hexdigest(),
                )
            )
    return observations


def parse_status_invest_income_events(html: str, *, ticker: str) -> list[IncomeEventObservation]:
    tree = HTMLParser(html)
    node = tree.css_first("#earning-section input#results")
    raw = node.attributes.get("value") if node is not None else None
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    observations: list[IncomeEventObservation] = []
    for ordinal, row in enumerate(payload):
        if not isinstance(row, dict):
            continue
        ex_date = _br_date(row.get("ed"))
        payment_date = _br_date(row.get("pd"))
        amount = _decimal(row.get("v"))
        event_type = _text(row.get("etd") or row.get("et")) or "Provento"
        if ex_date is None or payment_date is None or amount is None or amount <= 0:
            continue
        external = _text(row.get("id")) or str(ordinal)
        observations.append(
            IncomeEventObservation(
                source="status_invest",
                lineage="aggregator:status_invest",
                source_event_id=f"status-invest:{ticker}:{external}:{ex_date.isoformat()}",
                ticker=ticker,
                event_type=event_type,
                ex_date=ex_date,
                payment_date=payment_date,
                unit_price=amount,
                reference_period=_text(row.get("et")),
                authority=20,
                payload_hash=_payload_hash(row),
            )
        )
    return observations


def parse_cvm_income_report_text(
    text: str,
    *,
    ticker: str,
    document_id: str,
    version: int,
) -> list[IncomeEventObservation]:
    normalized = _normalize_pdf_text(text)
    ex_date = _cvm_ex_date(normalized)
    table = _after_label(normalized, "Código ISIN")
    payment_dates = _dates(table)
    payment_date = payment_dates[0] if payment_dates else None
    if ex_date is None or payment_date is None:
        return []
    matches = list(
        re.finditer(
            r"\b(BR[A-Z0-9]{10})\b\s+([0-9]+(?:[.,][0-9]+)?)",
            table,
            flags=re.IGNORECASE,
        )
    )
    observations: list[IncomeEventObservation] = []
    for ordinal, match in enumerate(matches):
        amount = _decimal(match.group(2))
        if amount is None or amount <= 0:
            continue
        isin = match.group(1).upper()
        observations.append(
            IncomeEventObservation(
                source="cvm",
                lineage="official:cvm",
                source_event_id=f"cvm:{document_id}:{ordinal}",
                ticker=ticker,
                isin=isin,
                event_type="Provento",
                ex_date=ex_date,
                payment_date=payment_date,
                unit_price=amount,
                source_version=max(version, 1),
                authority=100,
                payload_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
    return observations


def _fnet_event_nodes(provento: ET.Element) -> list[tuple[str, ET.Element]]:
    aliases = (("Rendimento", "Rendimento"), ("Amortizacao", "Amortização"))
    return [(label, node) for tag, label in aliases if (node := provento.find(tag)) is not None]


def _cvm_ex_date(text: str) -> date | None:
    marker = "Ultimo dia de negociação com Direitos"
    segment = _after_label(text, marker)[:180]
    dates = _dates(segment)
    return dates[1] if len(dates) >= 2 else (dates[0] if dates else None)


def _after_label(text: str, label: str) -> str:
    folded = _fold(text)
    folded_label = _fold(label)
    position = folded.find(folded_label)
    return folded[position + len(folded_label) :] if position >= 0 else ""


def _normalize_pdf_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)
    # PDF table extraction may split an ISIN across adjacent lines.
    joined = re.sub(r"\b(BR[A-Z0-9]{6,9})\s*\n\s*([A-Z0-9]{1,4})\b", r"\1\2", joined)
    return re.sub(r"(\d+[,]\d{8,})\s*\n\s*(\d)\s*\n", r"\1\2\n", joined)


def _dates(value: str) -> list[date]:
    return [
        parsed
        for token in re.findall(r"\b\d{2}/\d{2}/\d{4}\b", value)
        if (parsed := _br_date(token))
    ]


def _xml_text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    return _text(child.text) if child is not None else None


def _br_date(value: Any) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def _iso_date(value: Any) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    normalized = text.replace(".", "").replace(",", ".") if "," in text else text
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _text(value: Any) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


__all__ = [
    "parse_b3_income_events",
    "parse_cvm_income_report_text",
    "parse_fundos_net_xml",
    "parse_status_invest_income_events",
]
