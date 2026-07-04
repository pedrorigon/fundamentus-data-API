from collections.abc import Callable
from datetime import date
from decimal import Decimal

from app.models import DividendPeriod
from app.parsers.details import parse_asset_details
from app.parsers.dividends import filter_dividends, parse_dividends


def test_parse_itub4_details_preserves_bank_specific_fields(
    fixture_html: Callable[[str], str],
) -> None:
    details = parse_asset_details(
        fixture_html("itub4_details.html"),
        "ITUB4",
        "https://www.fundamentus.com.br/detalhes.php?papel=ITUB4&h=1",
    )

    assert details.ticker == "ITUB4"
    assert details.company_name == "ITAUUNIBANCO PN N1"
    assert details.asset_type == "PN N1"
    assert details.quote == Decimal("42.74")
    assert details.quote_date == date(2026, 7, 3)
    assert details.enterprise_value is None
    assert details.sector == "Intermediários Financeiros"

    balance = next(
        section for section in details.sections if section.name == "Dados Balanço Patrimonial"
    )
    labels = {field.label for field in balance.fields}
    assert "Depósitos" in labels
    assert "Cart. de Crédito" in labels


def test_parse_wege3_details_preserves_industrial_fields(
    fixture_html: Callable[[str], str],
) -> None:
    details = parse_asset_details(
        fixture_html("wege3_details.html"),
        "WEGE3",
        "https://www.fundamentus.com.br/detalhes.php?papel=WEGE3&h=1",
    )

    assert details.ticker == "WEGE3"
    assert details.company_name is not None
    assert details.quote is not None
    labels = {field.label for section in details.sections for field in section.fields}
    assert "Dív. Bruta" in labels
    assert "Disponibilidades" in labels


def test_missing_optional_fields_return_null() -> None:
    html = """
    <html><body class="detalhes"><table class="w728">
      <tr>
        <td class="label"><span class="txt">Papel</span></td>
        <td class="data"><span class="txt">TEST3</span></td>
      </tr>
      <tr>
        <td class="label"><span class="txt">Valor da firma</span></td>
        <td class="data"><span class="txt">-</span></td>
      </tr>
    </table></body></html>
    """
    details = parse_asset_details(html, "TEST3", "http://example.local")
    assert details.company_name is None
    assert details.enterprise_value is None


def test_parse_dividends_and_filters(fixture_html: Callable[[str], str]) -> None:
    dividends = parse_dividends(fixture_html("itub4_dividends.html"), "ITUB4")
    assert len(dividends) > 100
    first = dividends[0]
    assert first.ex_date == date(2026, 6, 30)
    assert first.payment_date == date(2026, 8, 3)
    assert first.value == Decimal("0.0182")
    assert first.type == "JRS CAP PROPRIO"

    as_of = date(2026, 7, 4)
    future = filter_dividends(dividends, DividendPeriod.future, as_of)
    assert future
    assert all(item.is_future_payment for item in future)
    assert any(item.ex_date and item.ex_date <= as_of for item in future)

    past = filter_dividends(dividends, DividendPeriod.past, as_of)
    assert past
    assert all(item.payment_date and item.payment_date <= as_of for item in past)


def test_upcoming_ex_date_filter_with_announced_future_event() -> None:
    html = """
    <html><body class="proventos"><table id="resultado"><tbody>
      <tr>
        <td>10/07/2026</td><td>0,1044</td>
        <td>JRS CAP PROPRIO</td><td>10/03/2027</td><td>1</td>
      </tr>
      <tr>
        <td>19/06/2026</td><td>0,1044</td>
        <td>JRS CAP PROPRIO</td><td>10/03/2027</td><td>1</td>
      </tr>
    </tbody></table></body></html>
    """
    dividends = parse_dividends(html, "TEST3")
    upcoming = filter_dividends(dividends, DividendPeriod.upcoming_ex_date, date(2026, 7, 4))
    assert len(upcoming) == 1
    assert upcoming[0].is_future_ex_date is True
