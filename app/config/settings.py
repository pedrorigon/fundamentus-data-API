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
    status_invest_base_url: str = "https://statusinvest.com.br"
    cvm_open_data_base_url: str = "https://dados.cvm.gov.br"
    bcb_sgs_base_url: str = "https://api.bcb.gov.br"
    bcb_ifdata_base_url: str = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
    brapi_base_url: str = "https://brapi.dev"
    brapi_token: SecretStr | None = None
    alpha_vantage_base_url: str = "https://www.alphavantage.co"
    alpha_vantage_api_key: SecretStr | None = None
    # Public annual statements for foreign listings. Alpha Vantage covers the
    # same ground but requires a per-user API key, so it cannot be the default
    # source for a self-hosted deployment.
    yahoo_fundamentals_base_url: str = "https://query2.finance.yahoo.com"
    yahoo_quote_base_url: str = "https://query1.finance.yahoo.com"
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
    fixed_income_current_ttl_seconds: int = 3600
    fixed_income_history_ttl_seconds: int = 2592000
    equity_history_ttl_seconds: int = 315360000
    # Statements only change when a new filing is published.
    fundamentals_statements_ttl_seconds: int = 86400
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
