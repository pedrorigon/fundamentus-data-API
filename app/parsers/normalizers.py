import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

NULL_TOKENS = {"", "-", "--", "n/a", "na", "nd", "n.d.", "indisponivel", "indisponível"}
TICKER_RE = re.compile(r"^[A-Z0-9]{4,12}$")


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_key(label: str) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(label))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_text).strip("_").lower()
    return key or "campo"


def normalize_ticker(ticker: str) -> str:
    normalized = clean_text(ticker).upper()
    if not TICKER_RE.fullmatch(normalized):
        raise ValueError("invalid ticker")
    return normalized


def is_nullish(value: str | None) -> bool:
    text = clean_text(value)
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return normalized.lower() in NULL_TOKENS


def parse_br_date(value: str | None) -> date | None:
    if is_nullish(value):
        return None
    text = clean_text(value)
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_br_decimal(value: str | None) -> Decimal | None:
    if is_nullish(value):
        return None
    text = clean_text(value)
    text = text.replace("R$", "").replace("%", "").strip()
    if re.search(r"[A-Za-zÀ-ÿ]", text):
        return None
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-+]", "", text)
    if text in {"", "-", "+", "."}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def infer_value(raw_value: str | None, label: str = "") -> tuple[object | None, str]:
    raw = clean_text(raw_value)
    if is_nullish(raw):
        return None, "null"
    parsed_date = parse_br_date(raw)
    if parsed_date is not None:
        return parsed_date, "date"
    parsed_decimal = parse_br_decimal(raw)
    if parsed_decimal is not None:
        lower_label = normalize_key(label)
        if "%" in raw:
            return parsed_decimal, "percent"
        if raw.startswith("R$") or any(
            token in lower_label for token in ("valor", "cotacao", "volume", "vpa", "lpa", "preco")
        ):
            return parsed_decimal, "money"
        return parsed_decimal, "number"
    return raw or None, "text"
