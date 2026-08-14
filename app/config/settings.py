from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FUNDAMENTUS_API_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Fundamentus Data API"
    environment: str = "local"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    timezone: str = "America/Sao_Paulo"

    fundamentus_base_url: str = "https://www.fundamentus.com.br"
    b3_bdi_base_url: str = "https://arquivos.b3.com.br/bdi"
    b3_historical_quote_base_url: str = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist"
    anbima_debenture_base_url: str = (
        "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs"
    )
    # ANBIMA Data publishes the latest five business days of CRI/CRA
    # indicative prices through this public page.  The page is intentionally
    # kept separate from the authenticated ANBIMA Feed API.
    anbima_credit_url: str = (
        "https://www.anbima.com.br/pt_br/informar/precos-e-indices/precos/"
        "taxas-de-cri-e-cra/taxas-de-cri-e-cra.htm"
    )
    status_invest_base_url: str = "https://statusinvest.com.br"
    cvm_open_data_base_url: str = "https://dados.cvm.gov.br"
    bcb_sgs_base_url: str = "https://api.bcb.gov.br"
    bcb_ifdata_base_url: str = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
    brapi_base_url: str = "https://brapi.dev"
    brapi_token: SecretStr | None = None
    # The public brapi quote list is a complementary directory for B3 assets.
    # It is fetched in one bounded refresh and never queried by search.
    brapi_instrument_directory_path: str = "/api/quote/list"
    instrument_directory_ttl_seconds: int = 86400
    instrument_directory_max_rows: int = 1000
    instrument_directory_max_entries: int = 20000
    alpha_vantage_base_url: str = "https://www.alphavantage.co"
    alpha_vantage_api_key: SecretStr | None = None
    # SEC EDGAR CompanyFacts is public and does not require an API key. SEC
    # asks clients to identify themselves with a descriptive User-Agent.
    sec_edgar_base_url: str = "https://data.sec.gov"
    sec_company_tickers_url: str = "https://www.sec.gov/files/company_tickers_exchange.json"
    sec_user_agent: str | None = None
    sec_request_timeout_seconds: float = 10.0
    sec_companyfacts_ttl_seconds: int = 86400
    sec_ticker_map_ttl_seconds: int = 86400
    # Public statement pages for foreign listings. Alpha Vantage covers the
    # same ground but requires a per-user API key, so it cannot be the default
    # source for a self-hosted deployment.
    stock_analysis_base_url: str = "https://stockanalysis.com"
    investidor10_base_url: str = "https://investidor10.com.br"
    bazin_minimum_yield_percent: Decimal = Decimal("6")
    user_agent: str = (
        f"fundamentus-data-api/{__version__} local scraper "
        "(contact: local-development; purpose: personal local API)"
    )
    request_timeout_seconds: float = 10.0
    # CVM archives are tens of megabytes, so they need a longer budget than
    # the HTML scrapers.
    cvm_request_timeout_seconds: float = 120.0
    max_connections: int = 8
    max_keepalive_connections: int = 4
    upstream_concurrency: int = 4
    upstream_min_interval_seconds: float = 0.15

    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    circuit_breaker_failures: int = 5
    circuit_breaker_recovery_seconds: float = 60.0

    market_data_ttl_seconds: int = 300
    fundamentals_ttl_seconds: int = 3600
    dividends_ttl_seconds: int = 21600
    cache_headers_max_age_seconds: int = 60
    opportunity_cache_ttl_seconds: int = 900
    instrument_data_ttl_seconds: int = 86400
    ticker_cache_max_entries: int = 1024
    fixed_income_current_ttl_seconds: int = 3600
    fixed_income_history_ttl_seconds: int = 2592000
    equity_history_ttl_seconds: int = 315360000
    # Statements only change when a new filing is published.
    fundamentals_statements_ttl_seconds: int = 86400
    # A closed exercise no longer changes, so its archive is kept for ten years
    # instead of being re-downloaded and re-decoded every day.
    closed_statements_ttl_seconds: int = 315360000
    # Resolved company filings are rebuilt daily so a newly published quarterly
    # statement is picked up, while a refresh within the day skips the archives.
    resolved_company_ttl_seconds: int = 86400
    company_registry_ttl_seconds: int = 604800
    peer_group_ttl_seconds: int = 86400
    fundamentals_history_years: int = 12

    sqlite_cache_enabled: bool = True
    sqlite_cache_path: Path = Field(default=Path(".cache/fundamentus_cache.sqlite3"))

    batch_limit: int = 20
    cache_invalidate_token: SecretStr | None = None

    @property
    def details_ttl_seconds(self) -> int:
        return min(self.market_data_ttl_seconds, self.fundamentals_ttl_seconds)


@lru_cache
def get_settings() -> Settings:
    return Settings()
